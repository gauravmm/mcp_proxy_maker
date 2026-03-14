"""Notion content-based access control plugin.

Enforces per-bot, per-page permissions using markers embedded in the first line
of each page's content. Marker format: ``BotName 🖊`` (read-write) or
``BotName 👀`` (read-only), appearing anywhere on the first line.

Also provides a ``notion_upload_image`` synthetic tool (when ``notion_token`` is
configured) that uploads a local image file to Notion and replaces a placeholder
block with an image block.
"""

from __future__ import annotations

import enum
import json
import logging
import mimetypes
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import mcp.types as mt
from fastmcp.tools.tool import Tool, ToolResult
from mcp import McpError
from mcp.types import ErrorData

from ..config.schema import NotionAccessPluginConfig
from .base import PluginBase

if TYPE_CHECKING:
    from fastmcp import FastMCP

_ERR_ACCESS_DENIED = -32601
_NOTION_API = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"
_PLACEHOLDER_PREFIX = "IMAGE_UPLOAD:"

# Tools that don't operate on specific pages
_PASSTHROUGH_TOOLS = {"notion-search", "notion-get-teams", "notion-get-users"}

# Tools that require WRITE access on a page
_WRITE_TOOLS = {"notion-update-page", "notion-create-comment", "notion-duplicate-page"}


class AccessLevel(enum.Enum):
    NONE = 0
    READ = 1
    WRITE = 2


@dataclass
class CachedPermission:
    level: AccessLevel
    first_line: str
    expires_at: float


def _extract_notion_page_text(content: str) -> str:
    """Extract the inner page text from a Notion fetch response.

    The Notion MCP returns tool results as a JSON object whose ``text`` field
    contains the human-readable page representation with ``<content>`` tags.
    Newlines inside that field are JSON-escaped, so the outer string has no
    actual newline characters.  We detect this case by checking whether
    ``content`` is valid JSON with a ``text`` key; if so, we unwrap it before
    applying the ``<content>`` regex.
    """
    try:
        obj = json.loads(content)
        if isinstance(obj, dict) and "text" in obj:
            return str(obj["text"])
    except (json.JSONDecodeError, ValueError):
        pass
    return content


def _parse_permission(
    content: str,
    bot_name: str,
    read_emoji: str,
    write_emoji: str,
) -> tuple[AccessLevel, str]:
    """Parse the first line of page content for bot permission markers.

    Returns (access_level, first_line_text).
    """
    # Notion fetch responses are JSON objects; extract the inner text first.
    text = _extract_notion_page_text(content)

    # The inner text wraps page content in <content>...</content>.
    # Extract the first line after the opening tag.
    content_match = re.search(r"<content>\n(.*?)(?:\n|$)", text)
    if content_match:
        first_line = content_match.group(1)
    else:
        # Fallback: just use the first line of the text
        first_line = text.split("\n")[0] if text else ""

    has_write = f"{bot_name} {write_emoji}" in first_line
    has_read = f"{bot_name} {read_emoji}" in first_line

    if has_write:
        return AccessLevel.WRITE, first_line
    if has_read:
        return AccessLevel.READ, first_line
    return AccessLevel.NONE, first_line


def _extract_text(result: ToolResult) -> str:
    """Extract concatenated text content from a ToolResult."""
    if not result.content:
        return ""
    parts = []
    for block in result.content:
        if isinstance(block, mt.TextContent):
            parts.append(block.text)
    return "\n".join(parts)


def _normalize_page_id(page_id: str) -> str:
    """Normalise a Notion page ID to dashed UUID format."""
    raw = page_id.replace("-", "")
    if len(raw) == 32:
        return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
    return page_id


def _extract_page_id_from_fetch_args(arguments: dict | None) -> str | None:
    """Extract the page ID from notion-fetch arguments."""
    if not arguments:
        return None
    # notion-fetch uses 'id' parameter (may contain hyphens or not)
    return arguments.get("id")


def _make_error_result(message: str) -> ToolResult:
    """Create a ToolResult with an error message."""
    return ToolResult(content=[mt.TextContent(type="text", text=f"[ACCESS DENIED] {message}")])


class NotionAccessPlugin(PluginBase):
    """Enforces per-bot page-level access control on a Notion MCP upstream."""

    def __init__(self, config: NotionAccessPluginConfig) -> None:
        self._bot_name = config.bot_name
        self._read_emoji = config.read_emoji
        self._write_emoji = config.write_emoji
        self._ttl = config.cache_ttl_seconds
        self._allow_workspace_creation = config.allow_workspace_creation
        self._block_tools = set(config.block_tools)
        self._cache: dict[str, CachedPermission] = {}
        self._notion_token = config.notion_token

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _get_cached(self, page_id: str) -> CachedPermission | None:
        """Return cached permission if present and not expired."""
        entry = self._cache.get(_normalize_page_id(page_id))
        if entry is None:
            return None
        if time.monotonic() > entry.expires_at:
            del self._cache[_normalize_page_id(page_id)]
            return None
        return entry

    def _set_cached(self, page_id: str, level: AccessLevel, first_line: str) -> None:
        self._cache[_normalize_page_id(page_id)] = CachedPermission(
            level=level,
            first_line=first_line,
            expires_at=time.monotonic() + self._ttl,
        )

    def _require_cached(self, page_id: str, required: AccessLevel) -> CachedPermission:
        """Return cached permission or raise if missing/insufficient."""
        entry = self._get_cached(page_id)
        if entry is None:
            raise McpError(
                ErrorData(
                    code=_ERR_ACCESS_DENIED,
                    message=(
                        f"Page {page_id} not in cache. Fetch the page first to check permissions."
                    ),
                )
            )
        if entry.level.value < required.value:
            level_name = "read-write" if required == AccessLevel.WRITE else "read"
            raise McpError(
                ErrorData(
                    code=_ERR_ACCESS_DENIED,
                    message=(
                        f"No {level_name} permission for {self._bot_name} on page {page_id}. "
                        f"Current access: {entry.level.name}."
                    ),
                )
            )
        return entry

    # ------------------------------------------------------------------
    # on_list_tools: remove blocked tools
    # ------------------------------------------------------------------

    async def on_list_tools(self, tools: list[Tool]) -> list[Tool]:
        return [t for t in tools if t.name not in self._block_tools]

    # ------------------------------------------------------------------
    # on_call_tool_request: route per tool
    # ------------------------------------------------------------------

    async def on_call_tool_request(
        self, params: mt.CallToolRequestParams
    ) -> mt.CallToolRequestParams:
        name = params.name
        args = params.arguments or {}

        # Blocked tools
        if name in self._block_tools:
            raise McpError(
                ErrorData(code=_ERR_ACCESS_DENIED, message=f"Tool {name} is blocked by policy.")
            )

        # Passthrough tools (no page-specific content)
        if name in _PASSTHROUGH_TOOLS:
            return params

        # notion-fetch: allow through, response hook handles permission check
        if name == "notion-fetch":
            return params

        # notion-get-comments: requires at least READ on the page
        if name == "notion-get-comments":
            page_id = args.get("page_id", "")
            self._require_cached(page_id, AccessLevel.READ)
            return params

        # Write tools that operate on a single page_id
        if name in _WRITE_TOOLS:
            page_id = args.get("page_id", "")
            cached = self._require_cached(page_id, AccessLevel.WRITE)

            # For notion-update-page: protect the first line
            if name == "notion-update-page":
                params = self._protect_first_line(params, cached)

            return params

        # notion-move-pages: requires WRITE on all pages being moved
        if name == "notion-move-pages":
            page_ids = args.get("page_or_database_ids", [])
            for pid in page_ids:
                self._require_cached(pid, AccessLevel.WRITE)
            return params

        # notion-create-pages: check parent permission
        if name == "notion-create-pages":
            return self._handle_create_pages_request(params)

        # Unknown tool: allow through
        return params

    def _protect_first_line(
        self, params: mt.CallToolRequestParams, cached: CachedPermission
    ) -> mt.CallToolRequestParams:
        """For update_content: block edits targeting the first line.
        For replace_content: prepend the cached first line."""
        args = dict(params.arguments or {})
        command = args.get("command", "")

        if command == "update_content":
            content_updates = args.get("content_updates", [])
            for update in content_updates:
                if isinstance(update, dict):
                    old_str = update.get("old_str", "")
                else:
                    old_str = getattr(update, "old_str", "")
                if old_str and cached.first_line and old_str in cached.first_line:
                    raise McpError(
                        ErrorData(
                            code=_ERR_ACCESS_DENIED,
                            message=(
                                "Cannot modify the permission marker line. "
                                "The first line contains access control markers for this page."
                            ),
                        )
                    )

        elif command == "replace_content":
            new_str = args.get("new_str", "")
            args["new_str"] = cached.first_line + "\n" + new_str
            return mt.CallToolRequestParams(name=params.name, arguments=args)

        return params

    def _strip_marker_line(self, content: str) -> str:
        """Remove a leading permission marker line if the LLM already included one."""
        if not content:
            return content
        first_line, _, rest = content.partition("\n")
        # Check if the first line looks like a permission marker for any bot
        if self._read_emoji in first_line or self._write_emoji in first_line:
            return rest
        return content

    def _handle_create_pages_request(
        self, params: mt.CallToolRequestParams
    ) -> mt.CallToolRequestParams:
        """Check parent permission for create-pages; inherit first line."""
        args = dict(params.arguments or {})
        parent = args.get("parent")

        if parent and isinstance(parent, dict):
            parent_page_id = parent.get("page_id", "")
            if parent_page_id:
                cached = self._require_cached(parent_page_id, AccessLevel.WRITE)
                # Inherit the permission first line into each new page
                pages = args.get("pages", [])
                new_pages = []
                for page in pages:
                    page = dict(page)
                    content = self._strip_marker_line(page.get("content", ""))
                    page["content"] = cached.first_line + "\n" + content
                    new_pages.append(page)
                args["pages"] = new_pages
                return mt.CallToolRequestParams(name=params.name, arguments=args)
        else:
            # No parent specified — workspace-level creation
            if not self._allow_workspace_creation:
                raise McpError(
                    ErrorData(
                        code=_ERR_ACCESS_DENIED,
                        message=(
                            "Workspace-level page creation is not allowed. "
                            "Specify a parent page_id."
                        ),
                    )
                )

        return params

    # ------------------------------------------------------------------
    # on_call_tool_response: inspect notion-fetch responses
    # ------------------------------------------------------------------

    async def on_call_tool_response(
        self,
        params: mt.CallToolRequestParams,
        result: ToolResult,
    ) -> ToolResult:
        if params.name != "notion-fetch":
            return result

        page_id = _extract_page_id_from_fetch_args(params.arguments)
        if not page_id:
            return result

        text = _extract_text(result)
        if not text:
            return result

        level, first_line = _parse_permission(
            text, self._bot_name, self._read_emoji, self._write_emoji
        )
        self._set_cached(page_id, level, first_line)

        if level == AccessLevel.NONE:
            return _make_error_result(f"No permission marker for {self._bot_name} on this page.")

        return result

    # ------------------------------------------------------------------
    # Synthetic tool registration
    # ------------------------------------------------------------------

    def register_tools(self, server: FastMCP) -> None:  # noqa: F821
        if not self._notion_token:
            logging.warning(
                "notion_access: notion_token not configured; "
                "notion_upload_image tool will not be available."
            )
            return
        _register_upload_tool(server, self, self._notion_token)


def _register_upload_tool(
    server: FastMCP,  # noqa: F821
    plugin: NotionAccessPlugin,
    token: str,
) -> None:
    """Register the notion_upload_image tool on the aggregator server."""

    @server.tool(name="notion_upload_image")
    async def notion_upload_image(
        page_id: str,
        file_path: str,
        caption: str = "",
    ) -> str:
        """Upload a local image to Notion and replace a placeholder block with an image block.

        Before calling this tool, insert the placeholder text
        ``[IMAGE_UPLOAD: /path/to/file.jpg]`` into the target page using
        ``notion-update-page``. This tool finds that paragraph block, uploads the
        image file to Notion, deletes the placeholder, and appends a proper image
        block in its place.

        The page must have been fetched with ``notion-fetch`` first so that WRITE
        permission is confirmed and cached.

        Args:
            page_id: ID of the Notion page to insert the image into.
            file_path: Absolute or relative path to the local image file.
            caption: Optional caption for the image block.
        """
        path = Path(file_path)
        if not path.exists():
            raise McpError(
                ErrorData(code=_ERR_ACCESS_DENIED, message=f"File not found: {file_path}")
            )

        page_id = _normalize_page_id(page_id)

        # Require cached WRITE permission — consistent with other write tools.
        plugin._require_cached(page_id, AccessLevel.WRITE)

        file_bytes = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        placeholder = f"[{_PLACEHOLDER_PREFIX} {file_path}]"

        json_headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": _NOTION_VERSION,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient() as client:
            # 1. Find the placeholder paragraph block.
            resp = await client.get(
                f"{_NOTION_API}/blocks/{page_id}/children",
                headers=json_headers,
            )
            resp.raise_for_status()
            blocks = resp.json().get("results", [])
            placeholder_block_id: str | None = None
            preceding_block_id: str | None = None
            prev_id: str | None = None
            for block in blocks:
                rich_text = block.get("paragraph", {}).get("rich_text", [])
                full_text = "".join(p.get("plain_text", "") for p in rich_text)
                if placeholder in full_text:
                    placeholder_block_id = block["id"]
                    preceding_block_id = prev_id
                    break
                prev_id = block["id"]

            if placeholder_block_id is None:
                raise McpError(
                    ErrorData(
                        code=_ERR_ACCESS_DENIED,
                        message=f"Placeholder '{placeholder}' not found in page {page_id}. "
                        "Insert it with notion-update-page before calling this tool.",
                    )
                )

            # 2. Create a Notion file upload session.
            resp = await client.post(
                f"{_NOTION_API}/file_uploads",
                headers=json_headers,
                json={"name": path.name, "content_type": content_type},
            )
            resp.raise_for_status()
            upload_data = resp.json()
            file_id: str = upload_data["id"]
            upload_url: str = upload_data["upload_url"]

            # 3. Upload the file bytes via multipart POST to the upload URL.
            resp = await client.post(
                upload_url,
                headers={"Authorization": f"Bearer {token}", "Notion-Version": _NOTION_VERSION},
                files={"file": (path.name, file_bytes, content_type)},
            )
            resp.raise_for_status()

            # 4. Delete the placeholder block.
            resp = await client.delete(
                f"{_NOTION_API}/blocks/{placeholder_block_id}",
                headers=json_headers,
            )
            resp.raise_for_status()

            # 5. Append an image block referencing the uploaded file.
            image_value: dict = {
                "type": "file_upload",
                "file_upload": {"id": file_id},
            }
            if caption:
                image_value["caption"] = [{"type": "text", "text": {"content": caption}}]
            image_block = {"object": "block", "type": "image", "image": image_value}
            resp = await client.patch(
                f"{_NOTION_API}/blocks/{page_id}/children",
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
