"""Logged-in Skool classroom walker + Mux signed-URL capturer.

Uses Playwright with a persistent Chrome profile (reuses the user's existing
Skool login). Two operating modes:

- auto: headless-ish, walks the classroom programmatically with randomized
  delays between lessons. Higher detection signal — use for low-priority
  courses.
- manual: launches a visible browser; the user clicks through lessons
  themselves. Script listens for Mux .m3u8 requests and captures the signed
  URL per lesson. Zero detection signal.

This module is the only piece that touches Skool's servers directly.
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


@dataclass
class LessonRef:
    """One lesson in a Skool classroom."""

    order: int
    title: str
    url: str
    slug: str = ""

    def __post_init__(self):
        if not self.slug:
            self.slug = _slugify(self.title)


@dataclass
class CapturedLesson:
    """A lesson with its captured signed Mux stream URL."""

    lesson: LessonRef
    mux_url: str
    mux_playback_id: str = ""
    page_html: str = field(default="", repr=False)


# Regex matches signed Mux URLs Skool uses:
# https://stream.video.skool.com/<playback_id>.m3u8?token=...
MUX_URL_RE = re.compile(
    r"https://stream\.video\.skool\.com/([A-Za-z0-9]+)\.m3u8\?token=[^\s\"'<>]+"
)


def _slugify(title: str) -> str:
    s = title.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[-\s]+", "-", s)
    return s.strip("-")[:80]


def _sleep_random(min_s: int = 30, max_s: int = 90) -> None:
    delay = random.uniform(min_s, max_s)
    log.info("Sleeping %.1fs before next lesson", delay)
    time.sleep(delay)


def launch_context(profile_dir: Path, headless: bool = False):
    """Launch a persistent Playwright Chromium context using the given profile.

    The profile_dir should be a copy of the user's real Chrome profile (or
    a profile that has been logged into Skool at least once interactively).

    Returns (playwright, context). Caller is responsible for closing both.
    """
    from playwright.sync_api import sync_playwright

    profile_dir.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=headless,
        viewport={"width": 1440, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        args=["--disable-blink-features=AutomationControlled"],
    )
    return pw, context


def list_lessons(classroom_url: str, context) -> list[LessonRef]:
    """Walk the classroom page and return the ordered list of lessons.

    Skool's classroom page renders lesson cards inside the course module.
    Each card is a link to /classroom/<course>/<lesson-slug>.
    """
    page = context.new_page()
    log.info("Loading classroom: %s", classroom_url)
    page.goto(classroom_url, wait_until="networkidle", timeout=60000)
    # Allow lazy-loaded lesson list to render
    page.wait_for_timeout(3000)

    # Heuristic: find all anchor tags pointing to /classroom/<course>/<something>
    anchors = page.evaluate(
        """
        () => {
            const links = Array.from(document.querySelectorAll('a[href*="/classroom/"]'));
            // Dedupe by href, preserve order
            const seen = new Set();
            const out = [];
            for (const a of links) {
                const href = a.href;
                if (seen.has(href)) continue;
                seen.add(href);
                // Skip the bare classroom root
                const parts = new URL(href).pathname.split('/').filter(Boolean);
                if (parts.length < 3) continue;
                const title = (a.innerText || a.textContent || '').trim().split('\\n')[0].trim();
                if (!title) continue;
                out.push({ href, title });
            }
            return out;
        }
        """
    )

    lessons: list[LessonRef] = []
    for i, item in enumerate(anchors, start=1):
        title = item["title"]
        if len(title) < 3:
            continue
        lessons.append(LessonRef(order=i, title=title, url=item["href"]))

    page.close()
    log.info("Found %d lesson candidates", len(lessons))
    return lessons


def capture_mux_url(lesson_url: str, context, wait_s: int = 20) -> CapturedLesson | None:
    """Open a lesson page and capture the first Mux signed URL seen.

    Returns None if no Mux URL is captured within wait_s seconds.
    """
    page = context.new_page()
    captured: dict[str, str] = {}

    def on_request(req):
        if "stream.video.skool.com" in req.url and ".m3u8" in req.url and "token=" in req.url:
            if "mux_url" not in captured:
                captured["mux_url"] = req.url
                log.info("Captured Mux URL for %s", lesson_url)

    page.on("request", on_request)
    log.info("Loading lesson: %s", lesson_url)
    page.goto(lesson_url, wait_until="domcontentloaded", timeout=60000)

    # Try to autoplay — click the video element if present
    try:
        page.wait_for_selector("video", timeout=10000)
        page.evaluate("document.querySelector('video')?.play()")
    except Exception:
        pass

    # Wait for Mux URL to appear
    deadline = time.time() + wait_s
    while time.time() < deadline and "mux_url" not in captured:
        page.wait_for_timeout(500)

    title = ""
    try:
        title = page.title().strip()
    except Exception:
        pass

    page.close()

    if "mux_url" not in captured:
        log.warning("No Mux URL captured for %s", lesson_url)
        return None

    mux_url = captured["mux_url"]
    m = MUX_URL_RE.search(mux_url)
    playback_id = m.group(1) if m else ""
    lesson = LessonRef(order=0, title=title or lesson_url, url=lesson_url)
    return CapturedLesson(lesson=lesson, mux_url=mux_url, mux_playback_id=playback_id)


def run_manual_mode(
    classroom_url: str,
    context,
    on_capture: Callable[[CapturedLesson], None],
    stop_after: int | None = None,
) -> None:
    """Open the classroom in a visible window and listen for Mux URLs as the
    user clicks through lessons.

    The script does NOT navigate — the user drives. We just intercept network
    requests on every page in the context and fire `on_capture` per unique
    lesson Mux URL.

    Returns when:
    - The user closes the browser, OR
    - `stop_after` distinct lessons have been captured.
    """
    seen_playback_ids: set[str] = set()
    pending: list[dict] = []
    processed_count = 0
    page = context.new_page()
    page.goto(classroom_url, wait_until="domcontentloaded", timeout=60000)
    log.info("Manual mode active. Click lessons in the browser. Ctrl+C to stop.")

    def on_request(req):
        # Lightweight handler: just capture URL + playback id. Title is
        # resolved later from the main loop so we can wait for the page
        # to render (the request fires before <h1>/<title> are populated).
        if "stream.video.skool.com" not in req.url or ".m3u8" not in req.url:
            return
        if "token=" not in req.url:
            return
        m = MUX_URL_RE.search(req.url)
        if not m:
            return
        pid = m.group(1)
        if pid in seen_playback_ids:
            return
        seen_playback_ids.add(pid)
        try:
            page_ref = req.frame.page
        except Exception:
            page_ref = None
        pending.append({"mux_url": req.url, "pid": pid, "page": page_ref})
        log.info("[%d] Mux URL captured — resolving title…", len(seen_playback_ids))

    def _resolve_title_and_url(page_ref) -> tuple[str, str]:
        if page_ref is None:
            return ("", "")
        try:
            # Give the SPA a moment to set h1/title after navigating
            page_ref.wait_for_timeout(1500)
            title = page_ref.evaluate(
                "() => (document.querySelector('h1')?.textContent || document.title || '').trim()"
            )
            return (title or "", page_ref.url or "")
        except Exception as e:
            log.debug("title resolution failed: %s", e)
            try:
                return ("", page_ref.url or "")
            except Exception:
                return ("", "")

    context.on("request", on_request)

    try:
        while True:
            if stop_after is not None and processed_count >= stop_after:
                log.info("Reached stop_after=%d, exiting manual mode", stop_after)
                return
            if len(context.pages) == 0:
                log.info("Browser closed, exiting manual mode")
                return
            # Drain any pending captures
            while pending:
                item = pending.pop(0)
                processed_count += 1
                title, url = _resolve_title_and_url(item["page"])
                lesson = LessonRef(
                    order=processed_count,
                    title=title or f"lesson-{processed_count}",
                    url=url,
                )
                captured = CapturedLesson(
                    lesson=lesson,
                    mux_url=item["mux_url"],
                    mux_playback_id=item["pid"],
                )
                log.info(
                    "[%d] Captured: %s (%s)",
                    processed_count,
                    lesson.title[:80],
                    item["pid"],
                )
                try:
                    on_capture(captured)
                except Exception as e:
                    log.exception("on_capture handler failed: %s", e)
                if stop_after is not None and processed_count >= stop_after:
                    return
            # IMPORTANT: use Playwright's wait, not time.sleep — sync_playwright
            # only delivers `request` events while the main thread is inside a
            # Playwright wait call. time.sleep would starve the event loop and
            # batch all captures until Ctrl+C.
            try:
                page.wait_for_timeout(500)
            except Exception:
                # Page closed; loop continues to check len(context.pages)
                time.sleep(0.5)
    except KeyboardInterrupt:
        log.info("Interrupted by user")
        return
