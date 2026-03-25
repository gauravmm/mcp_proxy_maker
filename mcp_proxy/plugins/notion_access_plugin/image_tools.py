"""Synthetic Notion image tools for the Notion access plugin."""

from __future__ import annotations

import logging
import mimetypes
import re
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from mcp import McpError
from mcp.types import ErrorData

from .api import API_URL, NOTION_VERSION, PLACEHOLDER_PREFIX

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from .core import NotionAccessPlugin


_NOTION_S3_IMAGE_RE = re.compile(
    r"!\[([^\]]*)\]"
    r"\("
    r"(https://prod-files-secure\.s3\.[a-z0-9-]+\.amazonaws\.com/"
    r"[0-9a-f-]+/"
    r"([0-9a-f-]+)/"
    r"([^?\s)]+)"
    r"\?[^)\s]*)"
    r"\)"
)


async def _do_upload(
    page_id: str,
    file_path: str,
    caption: str,
    token: str,
) -> str:
    """Find the IMAGE_UPLOAD placeholder on the page, upload the file, and replace it.

    Raises McpError if the placeholder is missing or the file does not exist.
    """
    from mcp.types import ErrorData

    from .core import _ERR_ACCESS_DENIED

    path = Path(file_path)
    if not path.exists():
        raise McpError(ErrorData(code=_ERR_ACCESS_DENIED, message=f"File not found: {file_path}"))

    file_bytes = path.read_bytes()
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    placeholder = f"[{PLACEHOLDER_PREFIX} {file_path}]"

    json_headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{API_URL}/blocks/{page_id}/children", headers=json_headers)
        resp.raise_for_status()
        blocks = resp.json().get("results", [])
        placeholder_block_id: str | None = None
        preceding_block_id: str | None = None
        prev_id: str | None = None
        other_placeholders: list[str] = []
        prefix_marker = f"[{PLACEHOLDER_PREFIX}"
        for block in blocks:
            rich_text = block.get("paragraph", {}).get("rich_text", [])
            full_text = "".join(part.get("plain_text", "") for part in rich_text)
            if placeholder in full_text:
                placeholder_block_id = block["id"]
                preceding_block_id = prev_id
                break
            if prefix_marker in full_text:
                other_placeholders.append(full_text.strip())
            prev_id = block["id"]

        if placeholder_block_id is None:
            if other_placeholders:
                hint = (
                    f" Found other placeholder(s): {other_placeholders}"
                    " -- the file_path must match exactly."
                )
            else:
                hint = (
                    " No IMAGE_UPLOAD placeholders found on this page."
                    " Insert one with notion-update-page first,"
                    f" e.g. content_updates: [{{old_str: '<line>',"
                    f" new_str: '<line>\\n{placeholder}'}}]."
                )
            raise McpError(
                ErrorData(
                    code=_ERR_ACCESS_DENIED,
                    message=f"Placeholder '{placeholder}' not found in page {page_id}.{hint}",
                )
            )

        resp = await client.post(
            f"{API_URL}/file_uploads",
            headers=json_headers,
            json={"name": path.name, "content_type": content_type},
        )
        resp.raise_for_status()
        upload_data = resp.json()
        file_id: str = upload_data["id"]
        upload_url: str = upload_data["upload_url"]

        resp = await client.post(
            upload_url,
            headers={"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION},
            files={"file": (path.name, file_bytes, content_type)},
        )
        resp.raise_for_status()

        resp = await client.delete(
            f"{API_URL}/blocks/{placeholder_block_id}",
            headers=json_headers,
        )
        resp.raise_for_status()

        image_value: dict = {"type": "file_upload", "file_upload": {"id": file_id}}
        if caption:
            image_value["caption"] = [{"type": "text", "text": {"content": caption}}]
        image_block = {"object": "block", "type": "image", "image": image_value}
        resp = await client.patch(
            f"{API_URL}/blocks/{page_id}/children",
            headers=json_headers,
            json={
                "children": [image_block],
                **({"after": preceding_block_id} if preceding_block_id else {}),
            },
        )
        if not resp.is_success:
            logging.error("notion block append failed %s: %s", resp.status_code, resp.text)
        resp.raise_for_status()

    return f"Image '{path.name}' uploaded and inserted into page {page_id}."


def register_upload_tool(server: FastMCP, plugin: NotionAccessPlugin, token: str) -> None:
    """Register the notion-upload-image tool on the aggregator server."""
    from .api import normalize_page_id
    from .core import (
        _ERR_ACCESS_DENIED,
        AccessLevel,
    )

    @server.tool(name="notion-upload-image")
    async def notion_upload_image(page_id: str, file_path: str, caption: str = "") -> str:
        page_id = normalize_page_id(page_id)
        await plugin._ensure_cached(page_id, AccessLevel.WRITE)
        resolved = plugin._resolve_upload_path(file_path)
        return await _do_upload(page_id, resolved, caption, token)


def register_delete_image_tool(server: FastMCP, plugin: NotionAccessPlugin, token: str) -> None:
    """Register the notion-delete-image tool on the aggregator server."""
    from .api import normalize_page_id
    from .core import (
        _ERR_ACCESS_DENIED,
        AccessLevel,
    )

    @server.tool(name="notion-delete-image")
    async def notion_delete_image(page_id: str, block_ids: list[str]) -> str:
        page_id = normalize_page_id(page_id)
        await plugin._ensure_cached(page_id, AccessLevel.WRITE)

        json_headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

        deleted: list[str] = []
        errors: list[str] = []

        async with httpx.AsyncClient() as client:
            list_resp = await client.get(
                f"{API_URL}/blocks/{page_id}/children", headers=json_headers
            )
            url_to_notion_block_id: dict[str, str] = {}
            if list_resp.is_success:
                for block in list_resp.json().get("results", []):
                    if block.get("type") == "image":
                        url = block.get("image", {}).get("file", {}).get("url", "")
                        if url:
                            url_to_notion_block_id[url.split("?")[0]] = block["id"]

            for raw_block_id in block_ids:
                block_id = raw_block_id.removeprefix("notion-image:").split("/")[0]
                actual_id: str | None = None
                cached_img = plugin._image_cache.get(page_id, {}).get(block_id)
                if cached_img:
                    base_url = cached_img.signed_url.split("?")[0]
                    actual_id = url_to_notion_block_id.get(base_url)

                if actual_id is None:
                    errors.append(f"{block_id}: image block not found on page")
                    continue

                try:
                    resp = await client.delete(
                        f"{API_URL}/blocks/{actual_id}",
                        headers=json_headers,
                    )
                    resp.raise_for_status()
                    deleted.append(block_id)
                    if page_id in plugin._image_cache:
                        plugin._image_cache[page_id].pop(block_id, None)
                except Exception as exc:
                    errors.append(f"{block_id}: {exc}")

        if errors and not deleted:
            raise McpError(
                ErrorData(
                    code=_ERR_ACCESS_DENIED,
                    message=f"All deletions failed: {'; '.join(errors)}",
                )
            )

        summary = f"Deleted {len(deleted)} image block(s) from page {page_id}."
        if errors:
            summary += f" Errors: {'; '.join(errors)}"
        return summary
