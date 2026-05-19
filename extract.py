#!/usr/bin/env python3
"""Skool course transcript pipeline — standalone executor (Mac-local).

End-to-end:
    Playwright (logged in)  →  capture signed Mux URLs per lesson
    yt-dlp                  →  download English .vtt subtitles
    clean_vtt               →  paragraphed plain text
    claude -p (Max plan)    →  structured markdown notes (Summary + verbatim)
    write to ~/skool-notes/{course-slug}/{NN-lesson-slug}.md
    notion_mirror (opt-in)  →  child pages under "The AI Ad Alchemists"

Output destination is **outside** this repo (defaults to ~/skool-notes/).
Sync back to your main AIOS repo on VPS with the rsync helper.

Usage:
    python extract.py \\
        --course-url "https://www.skool.com/mrpaidsocial/classroom/<course-slug>" \\
        --mode manual \\
        [--notion-sync] [--dry-run] [--force] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from skool import claude_summarizer, notion_mirror, playwright_walker, vtt_cleaner  # noqa: E402

PROMPTS_DIR = Path(__file__).resolve().parent / "skool" / "prompts"
LESSON_PROMPT = PROMPTS_DIR / "lesson_summary.md"

DEFAULT_NOTES_ROOT = Path.home() / "skool-notes"
DEFAULT_PROFILE = Path.home() / ".config" / "skool-extractor" / "chrome-profile"

log = logging.getLogger("skool_extract")


def _slugify(s: str, maxlen: int = 60) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[-\s]+", "-", s)
    return s.strip("-")[:maxlen] or "untitled"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _frontmatter(meta: dict) -> str:
    lines = ["---"]
    for k, v in meta.items():
        if v is None:
            continue
        if isinstance(v, str) and ("\n" in v or ":" in v):
            v = json.dumps(v)
        lines.append(f"{k}: {v}")
    lines.append("---\n")
    return "\n".join(lines)


def _course_title_from_url(url: str) -> str:
    path = url.rstrip("/").split("/")
    slug = path[-1] if path else "course"
    return slug.replace("-", " ").title()


def extract_course(args: argparse.Namespace) -> None:
    course_url = args.course_url.rstrip("/")
    # Strip query string before deriving the slug ("classroom/abc?md=xyz" → "abc")
    course_slug = urlparse(course_url).path.rstrip("/").split("/")[-1] or "course"
    course_title = args.course_title or _course_title_from_url(course_url)
    notes_root = Path(args.notes_root).expanduser().resolve()
    course_dir = notes_root / course_slug
    course_dir.mkdir(parents=True, exist_ok=True)
    log.info("Notes destination: %s", course_dir)

    profile_dir = Path(os.getenv("SKOOL_CHROME_PROFILE") or DEFAULT_PROFILE).expanduser()
    log.info("Using Chrome profile: %s", profile_dir)

    pw, context = playwright_walker.launch_context(
        profile_dir, headless=(args.mode == "auto" and args.headless)
    )
    captured_count = 0
    notion_course_page_id_state: dict[str, str] = {}

    def handle_capture(captured: playwright_walker.CapturedLesson) -> None:
        nonlocal captured_count
        captured_count += 1
        if args.limit and captured_count > args.limit:
            return
        try:
            _process_lesson(
                captured=captured,
                order=captured_count,
                course_dir=course_dir,
                course_title=course_title,
                course_slug=course_slug,
                args=args,
                notion_state=notion_course_page_id_state,
            )
        except Exception as e:
            log.exception("Failed lesson %d: %s", captured_count, e)

    try:
        if args.mode == "manual":
            playwright_walker.run_manual_mode(
                classroom_url=course_url,
                context=context,
                on_capture=handle_capture,
                stop_after=args.limit,
            )
        else:
            lessons = playwright_walker.list_lessons(course_url, context)
            log.info("Auto mode: %d lessons enumerated", len(lessons))
            for i, lesson in enumerate(lessons, start=1):
                if args.limit and i > args.limit:
                    break
                captured = playwright_walker.capture_mux_url(lesson.url, context)
                if not captured:
                    log.warning("Skip: no Mux URL for %s", lesson.title)
                    continue
                captured.lesson = lesson
                handle_capture(captured)
                if i < len(lessons):
                    delay = random.uniform(30, 90)
                    log.info("Auto-mode sleep %.1fs", delay)
                    time.sleep(delay)
    finally:
        try:
            context.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass

    log.info("Processed %d lessons → %s", captured_count, course_dir)
    print(f"\n✓ Done. Notes at: {course_dir}")
    print(f"  Sync to VPS: rsync -av {course_dir}/ <vps>:~/zeroarc-aios/reference/courses/mr-paid-social/{course_slug}/\n")


def _process_lesson(
    captured,
    order: int,
    course_dir: Path,
    course_title: str,
    course_slug: str,
    args: argparse.Namespace,
    notion_state: dict[str, str],
) -> None:
    lesson_title = captured.lesson.title or f"Lesson {order}"
    lesson_title = re.sub(r"\s*[|·•]\s*Skool.*$", "", lesson_title).strip()
    lesson_slug = _slugify(lesson_title)
    out_md = course_dir / f"{order:02d}-{lesson_slug}.md"

    if out_md.exists() and not args.force:
        log.info("Skip (exists): %s", out_md.name)
        return

    if args.dry_run:
        log.info("[dry-run] Would process: %s → %s", lesson_title, out_md.name)
        return

    vtt_path = course_dir / f"{order:02d}-{lesson_slug}.en.vtt"
    try:
        vtt_cleaner.download_vtt(captured.mux_url, vtt_path)
    except Exception as e:
        log.error("VTT download failed for %s: %s", lesson_title, e)
        return

    clean_text = vtt_cleaner.clean_vtt(vtt_path)
    if len(clean_text) < 100:
        log.warning("Short transcript (%d chars) for %s", len(clean_text), lesson_title)

    meta = claude_summarizer.LessonMeta(
        course_title=course_title,
        module_title=course_title,
        lesson_number=order,
        lesson_title=lesson_title,
        source_url=captured.lesson.url,
    )
    try:
        notes_md = claude_summarizer.summarize_lesson(
            clean_text=clean_text,
            lesson_meta=meta,
            prompt_template=LESSON_PROMPT,
        )
    except Exception as e:
        log.error("Summarization failed for %s: %s", lesson_title, e)
        return

    fm = {
        "course": course_title,
        "course_slug": course_slug,
        "lesson_number": order,
        "lesson_title": lesson_title,
        "lesson_slug": lesson_slug,
        "source_url": captured.lesson.url,
        "mux_playback_id": captured.mux_playback_id,
        "vtt_path": vtt_path.name,
        "extracted_at": _now_iso(),
        "summary_model": "claude (max-plan cli)",
    }
    out_md.write_text(_frontmatter(fm) + notes_md + "\n", encoding="utf-8")
    log.info("Wrote: %s", out_md.name)

    if args.notion_sync:
        parent = os.getenv("NOTION_AI_AD_ALCHEMISTS_PAGE_ID")
        if not parent:
            log.error("NOTION_AI_AD_ALCHEMISTS_PAGE_ID not set; skipping Notion sync")
            return
        course_page_id = notion_state.get("course_page_id")
        if not course_page_id:
            course_page_id = notion_mirror.ensure_course_page(parent, course_title)
            notion_state["course_page_id"] = course_page_id
        callout = f"Lesson {order} · {course_title} · extracted {_now_iso()[:10]}"
        try:
            page_id = notion_mirror.upsert_lesson_page(
                course_page_id=course_page_id,
                lesson_title=lesson_title,
                markdown=notes_md,
                frontmatter_callout=callout,
            )
            content = out_md.read_text(encoding="utf-8")
            content = content.replace(
                "---\n\n",
                f"notion_page_id: {page_id}\n---\n\n",
                1,
            )
            out_md.write_text(content, encoding="utf-8")
            log.info("Mirrored to Notion: %s (%s)", lesson_title, page_id)
        except Exception as e:
            log.exception("Notion mirror failed for %s: %s", lesson_title, e)


def main():
    p = argparse.ArgumentParser(description="Skool course extractor (standalone)")
    p.add_argument("--course-url", required=True)
    p.add_argument("--course-title")
    p.add_argument("--mode", choices=["manual", "auto"], default="manual")
    p.add_argument("--notion-sync", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--headless", action="store_true")
    p.add_argument(
        "--notes-root",
        default=str(DEFAULT_NOTES_ROOT),
        help=f"Where to write notes (default: {DEFAULT_NOTES_ROOT})",
    )
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    extract_course(args)


if __name__ == "__main__":
    main()
