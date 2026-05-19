"""Lesson summarization via the `claude` CLI (Max plan — zero per-call cost).

Pipes the cleaned transcript + metadata through `claude -p` using the
lesson_summary.md prompt template. Returns structured markdown matching
the schema in the prompt (Summary / Key concepts / Action items / Transcript).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import TypedDict

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 180


class LessonMeta(TypedDict):
    course_title: str
    module_title: str
    lesson_number: int | str
    lesson_title: str
    source_url: str


def summarize_lesson(
    clean_text: str,
    lesson_meta: LessonMeta,
    prompt_template: Path,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> str:
    """Generate structured markdown notes for a lesson.

    Args:
        clean_text: Paragraphed plain text from vtt_cleaner.clean_vtt().
        lesson_meta: Dict with course_title, module_title, lesson_number,
            lesson_title, source_url.
        prompt_template: Path to lesson_summary.md.
        timeout_s: Subprocess timeout.

    Returns:
        Markdown string starting with `## Summary`.

    Raises:
        RuntimeError: claude CLI missing, exited non-zero, or returned invalid output.
    """
    if shutil.which("claude") is None:
        raise RuntimeError(
            "`claude` CLI not found on PATH. Install Claude Code: "
            "https://docs.anthropic.com/en/docs/claude-code"
        )

    template = prompt_template.read_text(encoding="utf-8")
    prompt = _render(template, {**lesson_meta, "transcript": clean_text})

    log.info(
        "Summarizing lesson %s: %s (%d chars)",
        lesson_meta.get("lesson_number"),
        lesson_meta.get("lesson_title"),
        len(clean_text),
    )

    # `claude -p` reads the prompt as argv. Pass full prompt as the -p arg.
    # Note: very large prompts may need stdin instead; for typical lessons
    # (~10-30k chars) argv is fine.
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude CLI failed (exit {result.returncode}): {result.stderr.strip()[:500]}"
        )

    out = result.stdout.strip()
    out = _strip_codefence(out)
    if not out.startswith("## "):
        # Try to recover — find the first ## heading
        m = re.search(r"^##\s+", out, re.MULTILINE)
        if m:
            out = out[m.start():]
        else:
            raise RuntimeError(
                f"claude output missing expected `## Summary` heading. First 200 chars: {out[:200]!r}"
            )
    return out


def _render(template: str, vars: dict) -> str:
    """Minimal {{var}} substitution — no escaping, no logic."""
    out = template
    for k, v in vars.items():
        out = out.replace("{{" + k + "}}", str(v))
    return out


def _strip_codefence(text: str) -> str:
    """If the whole output is wrapped in ```...```, strip it."""
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            # Drop first and last fence lines
            return "\n".join(lines[1:-1]).strip()
    return text


if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(sys.argv) < 4:
        print("Usage: claude_summarizer.py <clean.txt> <meta.json> <prompt.md>")
        sys.exit(1)
    clean = Path(sys.argv[1]).read_text(encoding="utf-8")
    meta = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    prompt = Path(sys.argv[3])
    print(summarize_lesson(clean, meta, prompt))
