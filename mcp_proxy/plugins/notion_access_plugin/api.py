"""Shared Notion API constants and lightweight helpers."""

from __future__ import annotations

import re

API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
PLACEHOLDER_PREFIX = "IMAGE_UPLOAD:"

IMAGE_PLACEHOLDER_RE = re.compile(r"!\[[^\]]*\]\(notion-image:[^)]+\)\n?")
UPLOAD_PLACEHOLDER_RE = re.compile(r"\[IMAGE_UPLOAD:\s*([^\]]+)\]")


def normalize_page_id(page_id: str) -> str:
    """Normalise a Notion page ID to dashed UUID format."""
    raw = page_id.replace("-", "")
    if len(raw) == 32:
        return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
    return page_id


def extract_page_id_from_fetch_args(arguments: dict | None) -> str | None:
    """Extract the page ID from notion-fetch arguments."""
    if not arguments:
        return None
    return arguments.get("id")
