"""Mirror lesson notes to Notion under "The AI Ad Alchemists" parent page.

Page tree:
    The AI Ad Alchemists  (parent, pre-existing)
    └── <Course Title>      (child page, one per course)
        └── <Lesson Title>  (child page, one per lesson, body = markdown notes)

Idempotent: re-running with the same lesson title finds the existing page
and replaces its children (no duplicates).

Uses the official Notion REST API via `notion_client`. Requires
NOTION_API_TOKEN (or NOTION_API_KEY) in the environment.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from notion_client import Client

log = logging.getLogger(__name__)

NOTION_TOKEN = os.getenv("NOTION_API_TOKEN") or os.getenv("NOTION_API_KEY")
PARENT_PAGE_ID = os.getenv("NOTION_AI_AD_ALCHEMISTS_PAGE_ID")

# Notion block-children API limit per request
BLOCK_CHUNK = 90
# Notion rich-text content limit per element
RT_LIMIT = 1900


@dataclass
class NotionPageRef:
    page_id: str
    title: str


def _client() -> Client:
    if not NOTION_TOKEN:
        raise RuntimeError("NOTION_API_TOKEN (or NOTION_API_KEY) not set")
    return Client(auth=NOTION_TOKEN)


# ── Page management ─────────────────────────────────────────────────────────

def find_child_page(parent_page_id: str, title: str) -> NotionPageRef | None:
    """Return the first child page of parent_page_id whose title matches."""
    notion = _client()
    cursor = None
    target = title.strip().lower()
    while True:
        kwargs = {"block_id": parent_page_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.blocks.children.list(**kwargs)
        for block in resp.get("results", []):
            if block.get("type") != "child_page":
                continue
            t = block.get("child_page", {}).get("title", "").strip().lower()
            if t == target:
                return NotionPageRef(page_id=block["id"], title=title)
        if not resp.get("has_more"):
            return None
        cursor = resp.get("next_cursor")


def ensure_course_page(parent_page_id: str, course_title: str) -> str:
    """Find or create a child page for the course under the parent. Returns page_id."""
    existing = find_child_page(parent_page_id, course_title)
    if existing:
        log.info("Course page exists: %s", course_title)
        return existing.page_id

    notion = _client()
    log.info("Creating course page: %s", course_title)
    page = notion.pages.create(
        parent={"page_id": parent_page_id},
        properties={
            "title": [{"type": "text", "text": {"content": course_title[:200]}}]
        },
    )
    return page["id"]


def upsert_lesson_page(
    course_page_id: str,
    lesson_title: str,
    markdown: str,
    frontmatter_callout: str | None = None,
) -> str:
    """Create or update a lesson page under the course.

    If a page with this title exists, its children are replaced (no duplicates).
    `frontmatter_callout` is an optional one-line summary rendered as a callout
    block above the main content.

    Returns the lesson page_id.
    """
    notion = _client()
    existing = find_child_page(course_page_id, lesson_title)

    if existing:
        page_id = existing.page_id
        log.info("Lesson page exists, replacing content: %s", lesson_title)
        _purge_children(page_id)
    else:
        log.info("Creating lesson page: %s", lesson_title)
        page = notion.pages.create(
            parent={"page_id": course_page_id},
            properties={
                "title": [{"type": "text", "text": {"content": lesson_title[:200]}}]
            },
        )
        page_id = page["id"]

    blocks = []
    if frontmatter_callout:
        blocks.append(_callout_block(frontmatter_callout))
    blocks.extend(_markdown_to_blocks(markdown))

    _append_blocks_chunked(page_id, blocks)
    return page_id


def _purge_children(page_id: str) -> None:
    """Delete (archive) all child blocks of a page."""
    notion = _client()
    cursor = None
    to_archive: list[str] = []
    while True:
        kwargs = {"block_id": page_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.blocks.children.list(**kwargs)
        for block in resp.get("results", []):
            to_archive.append(block["id"])
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    for bid in to_archive:
        try:
            notion.blocks.delete(block_id=bid)
        except Exception as e:
            log.warning("Failed to delete block %s: %s", bid, e)


def _append_blocks_chunked(page_id: str, blocks: list[dict]) -> None:
    notion = _client()
    for i in range(0, len(blocks), BLOCK_CHUNK):
        chunk = blocks[i : i + BLOCK_CHUNK]
        notion.blocks.children.append(block_id=page_id, children=chunk)


# ── Markdown → Notion blocks ────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_NUMBERED_RE = re.compile(r"^\s*\d+\.\s+(.*)$")


def _markdown_to_blocks(md: str) -> list[dict]:
    """Convert markdown to Notion blocks.

    Supports: # / ## / ### headings, paragraphs, bullet lists, numbered lists,
    blank-line paragraph breaks. Everything else → paragraph with text as-is.
    Long paragraphs are split to respect the 2000-char rich-text limit.
    """
    blocks: list[dict] = []
    lines = md.splitlines()
    para_buf: list[str] = []

    def flush_para():
        nonlocal para_buf
        if not para_buf:
            return
        text = " ".join(line.strip() for line in para_buf).strip()
        para_buf = []
        if not text:
            return
        for chunk in _split_text(text, RT_LIMIT):
            blocks.append(_paragraph_block(chunk))

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            flush_para()
            continue
        m = _HEADING_RE.match(line)
        if m:
            flush_para()
            level = len(m.group(1))
            blocks.append(_heading_block(m.group(2).strip(), level))
            continue
        m = _BULLET_RE.match(line)
        if m:
            flush_para()
            blocks.append(_bullet_block(m.group(1).strip()))
            continue
        m = _NUMBERED_RE.match(line)
        if m:
            flush_para()
            blocks.append(_numbered_block(m.group(1).strip()))
            continue
        para_buf.append(line)

    flush_para()
    return blocks


def _split_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    while text:
        if len(text) <= limit:
            out.append(text)
            break
        # Try to break at a space near the limit
        cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return out


def _rt(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": text[:RT_LIMIT]}}]


def _paragraph_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": _rt(text)},
    }


def _heading_block(text: str, level: int) -> dict:
    htype = {1: "heading_1", 2: "heading_2", 3: "heading_3"}.get(level, "heading_3")
    return {
        "object": "block",
        "type": htype,
        htype: {"rich_text": _rt(text)},
    }


def _bullet_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": _rt(text)},
    }


def _numbered_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "numbered_list_item",
        "numbered_list_item": {"rich_text": _rt(text)},
    }


def _callout_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": _rt(text),
            "icon": {"type": "emoji", "emoji": "📚"},
        },
    }
