"""WebVTT subtitle download + cleanup.

Downloads .vtt files from Mux signed URLs via yt-dlp, then strips timestamps
and deduplicates rolling captions to produce paragraphed plain text suitable
for LLM summarization.

No external dependencies beyond yt-dlp (CLI) and Python stdlib.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

REFERER = "https://www.skool.com/"
ORIGIN = "https://www.skool.com"

# Sentence-ending punctuation used as paragraph-break heuristic
_SENTENCE_END = re.compile(r"[.!?]\s*$")


def download_vtt(mux_url: str, out_path: Path, lang: str = "en") -> Path:
    """Run yt-dlp to fetch the English VTT subtitle track from a signed Mux URL.

    Args:
        mux_url: Full signed URL like
            https://stream.video.skool.com/{playback_id}.m3u8?token=...
        out_path: Where to write the .vtt file. Parent dir must exist.
        lang: Subtitle language code (default "en").

    Returns:
        Path to the written .vtt file.

    Raises:
        RuntimeError: yt-dlp failed (non-zero exit) or output file missing.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # yt-dlp writes to <template>.<lang>.vtt — strip .vtt from out_path for the template
    template = str(out_path).removesuffix(".vtt")
    if template.endswith(f".{lang}"):
        template = template[: -(len(lang) + 1)]

    cmd = [
        "yt-dlp",
        "--write-subs",
        "--sub-langs",
        lang,
        "--skip-download",
        "--referer",
        REFERER,
        "--add-header",
        f"Origin:{ORIGIN}",
        "-o",
        f"{template}.%(ext)s",
        mux_url,
    ]

    log.info("Downloading VTT for %s", out_path.name)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(
            f"yt-dlp failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    written = Path(f"{template}.{lang}.vtt")
    if not written.exists() or written.stat().st_size == 0:
        raise RuntimeError(f"VTT not written or empty: {written}")

    # Normalize to the caller-requested path if it differs
    if written != out_path:
        written.rename(out_path)
    return out_path


def clean_vtt(vtt_path: Path) -> str:
    """Parse a WebVTT file and return clean paragraphed plain text.

    Strips:
    - WEBVTT header
    - Cue numbers
    - Timestamps (00:00:00.000 --> 00:00:03.600)
    - WebVTT settings (e.g. "align:start position:0%")
    - HTML/styling tags inside cues
    - Duplicate / rolling-caption lines

    Returns:
        Plain text with paragraph breaks at sentence boundaries.
    """
    raw = vtt_path.read_text(encoding="utf-8")
    lines = raw.splitlines()

    cues: list[str] = []
    current: list[str] = []
    in_cue = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if current:
                cues.append(" ".join(current).strip())
                current = []
            in_cue = False
            continue
        if stripped.upper().startswith("WEBVTT"):
            continue
        if stripped.upper().startswith(("NOTE", "STYLE", "REGION")):
            continue
        # Timestamp line, e.g. "00:00:00.000 --> 00:00:03.600 align:start"
        if "-->" in stripped:
            in_cue = True
            continue
        # Cue identifier (a bare number or arbitrary id BEFORE a timestamp).
        # If we haven't entered a cue yet AND the line is a simple identifier, skip.
        if not in_cue and re.fullmatch(r"[\w\-\.]+", stripped) and not current:
            # Only skip if next non-empty line is a timestamp — but we can't easily peek here.
            # Heuristic: if it's purely digits or kebab-case id, skip.
            if re.fullmatch(r"\d+", stripped) or re.fullmatch(r"[\w\-]+", stripped):
                continue
        # Strip inline HTML/VTT tags like <c>, <00:00:01.000>
        text = re.sub(r"<[^>]+>", "", stripped)
        current.append(text)

    if current:
        cues.append(" ".join(current).strip())

    # Deduplicate consecutive identical cues (some players emit rolling captions)
    deduped: list[str] = []
    for cue in cues:
        if not cue:
            continue
        if deduped and cue == deduped[-1]:
            continue
        # Some VTTs have rolling overlap: "A B C" then "B C D" — try to dedupe overlap
        if deduped:
            prev = deduped[-1]
            # If cue starts with the tail of prev, only append the new portion
            overlap = _longest_overlap(prev, cue)
            if overlap and overlap > 8:
                cue = cue[overlap:].lstrip()
                if not cue:
                    continue
        deduped.append(cue)

    # Join cues into a single stream of sentences, then paragraph every ~5 sentences
    joined = " ".join(deduped)
    joined = re.sub(r"\s+", " ", joined).strip()

    sentences = _split_sentences(joined)
    paragraphs: list[str] = []
    buf: list[str] = []
    for sent in sentences:
        buf.append(sent)
        if len(buf) >= 5:
            paragraphs.append(" ".join(buf))
            buf = []
    if buf:
        paragraphs.append(" ".join(buf))

    return "\n\n".join(paragraphs)


def _longest_overlap(a: str, b: str, max_check: int = 80) -> int:
    """Return length of the longest suffix of `a` that is also a prefix of `b`."""
    n = min(len(a), len(b), max_check)
    for k in range(n, 0, -1):
        if a.endswith(b[:k]):
            return k
    return 0


def _split_sentences(text: str) -> list[str]:
    """Naive sentence splitter — splits on . ! ? followed by space + capital."""
    # Keep the delimiter attached to the preceding sentence
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
    return [p.strip() for p in parts if p.strip()]


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(sys.argv) < 2:
        print("Usage: python vtt_cleaner.py <path-to.vtt>")
        sys.exit(1)
    print(clean_vtt(Path(sys.argv[1])))
