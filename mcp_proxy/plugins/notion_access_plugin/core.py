"""Notion content-based access control plugin."""

from __future__ import annotations

import enum
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import mcp.types as mt
from fastmcp import Client
from fastmcp.tools.tool import ToolResult
from mcp import McpError
from mcp.types import ErrorData

from ...config.schema import NotionAccessPluginConfig
from ..base import PluginBase
from .image_tools import _NOTION_S3_IMAGE_RE

if TYPE_CHECKING:
    from fastmcp import Client, FastMCP
    from fastmcp.client.client import CallToolResult

_ERR_ACCESS_DENIED = -32601


_WRITE_TOOLS = {"notion-update-page", "notion-create-comment", "notion-duplicate-page"}


class AccessLevel(enum.Enum):
    NONE = 0
    READ = 1
    WRITE = 2


@dataclass
class CachedImage:
    filename: str
    alt_text: str
    signed_url: str


@dataclass
class CachedPermission:
    level: AccessLevel
    first_line: str
    expires_at: float


def _extract_notion_page_text(content: str) -> str:
    """Extract the inner page text from a Notion fetch response."""
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
    """Parse the first line of page content for bot permission markers."""
    text = _extract_notion_page_text(content)
    content_match = re.search(r"<content>\n(.*?)(?:\n|$)", text)
    if content_match:
        first_line = content_match.group(1)
    else:
        first_line = text.split("\n")[0] if text else ""

    has_write = f"{bot_name} {write_emoji}" in first_line
    has_read = f"{bot_name} {read_emoji}" in first_line

    if has_write:
        return AccessLevel.WRITE, first_line
    if has_read:
        return AccessLevel.READ, first_line
    return AccessLevel.NONE, first_line


def _extract_text(result: ToolResult | CallToolResult) -> str:
    """Extract concatenated text content from a tool result."""
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
    return arguments.get("id")


def _contains_image_placeholder(text: str) -> bool:
    """Return True when the text contains a notion-image placeholder."""
    return isinstance(text, str) and _IMAGE_PLACEHOLDER_RE.search(text) is not None


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
        self._block_tools = set(config.block_tools)
        self._cache: dict[str, CachedPermission] = {}
        self._image_cache: dict[str, dict[str, CachedImage]] = {}
        self._notion_token = config.notion_token
        self._client: Client | None = None

    def _get_cached(self, page_id: str) -> CachedPermission | None:
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

    def set_upstream_client(self, client: object) -> None:
        if isinstance(client, Client):
            self._client = client

    async def _auto_fetch(self, page_id: str) -> None:
        assert self._client is not None
        try:
            result = await self._client.call_tool("notion-fetch", {"id": page_id})
            text = _extract_text(result)
            if text:
                level, first_line = _parse_permission(
                    text, self._bot_name, self._read_emoji, self._write_emoji
                )
                self._set_cached(page_id, level, first_line)
        except Exception as exc:
            logging.warning("notion_access: auto-fetch failed for page %s: %s", page_id, exc)

    async def _ensure_cached(self, page_id: str, required: AccessLevel) -> CachedPermission:
        entry = self._get_cached(page_id)
        if entry is None:
            if self._client is not None:
                await self._auto_fetch(page_id)
                entry = self._get_cached(page_id)
            if entry is None:
                raise McpError(
                    ErrorData(
                        code=_ERR_ACCESS_DENIED,
                        message=(
                            f"Page {page_id} not cached. Fetch the page first to check permissions."
                        ),
                    )
                )
        if entry.level == AccessLevel.NONE:
            raise McpError(
                ErrorData(
                    code=_ERR_ACCESS_DENIED,
                    message=f"No permission marker for {self._bot_name} on page {page_id}.",
                )
            )
        if entry.level.value < required.value:
            raise McpError(
                ErrorData(
                    code=_ERR_ACCESS_DENIED,
                    message=(
                        f"No {required.name} permission for {self._bot_name} on page {page_id}. "
                        f"Current access: {entry.level.name}."
                    ),
                )
            )
        return entry

    def is_tool_allowed(self, name: str) -> bool:
        return name not in self._block_tools

    async def on_call_tool_request(
        self, params: mt.CallToolRequestParams
    ) -> mt.CallToolRequestParams:
        name = params.name
        args = params.arguments or {}

        if name in self._block_tools:
            raise McpError(
                ErrorData(code=_ERR_ACCESS_DENIED, message=f"Tool {name} is blocked by policy.")
            )

        if name == "notion-get-comments":
            await self._ensure_cached(args.get("page_id", ""), AccessLevel.READ)
            return params
        if name in _WRITE_TOOLS:
            cached = await self._ensure_cached(args.get("page_id", ""), AccessLevel.WRITE)
            if name == "notion-update-page":
                params = self._protect_first_line(params, cached)
        elif name == "notion-move-pages":
            for pid in args.get("page_or_database_ids", []):
                await self._ensure_cached(pid, AccessLevel.WRITE)
        elif name == "notion-create-pages":
            return await self._handle_create_pages_request(params)

        return params

    def _protect_first_line(
        self, params: mt.CallToolRequestParams, cached: CachedPermission
    ) -> mt.CallToolRequestParams:
        args = dict(params.arguments or {})
        command = args.get("command", "")
        page_id = args.get("page_id", "")
        has_cached_images = bool(self._image_cache.get(_normalize_page_id(page_id)))

        if command == "update_content":
            for update in args.get("content_updates", []):
                assert isinstance(update, dict)
                old_str = update.get("old_str", "")
                new_str = update.get("new_str", "")

                if _contains_image_placeholder(old_str) or _contains_image_placeholder(new_str):
                    raise McpError(
                        ErrorData(
                            code=_ERR_ACCESS_DENIED,
                            message=(
                                "Text edits cannot target notion-image placeholders. "
                                "Use notion-delete-image and notion-upload-image for image changes."
                            ),
                        )
                    )

                if old_str and (old_str in cached.first_line or cached.first_line in old_str):
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
            if has_cached_images:
                raise McpError(
                    ErrorData(
                        code=_ERR_ACCESS_DENIED,
                        message=(
                            "replace_content is blocked on pages with images. "
                            "Delete the image blocks with notion-delete-image "
                            "before replacing page text."
                        ),
                    )
                )
            if _contains_image_placeholder(new_str):
                raise McpError(
                    ErrorData(
                        code=_ERR_ACCESS_DENIED,
                        message=(
                            "Text edits cannot include notion-image placeholders. "
                            "Use notion-delete-image and notion-upload-image for image changes."
                        ),
                    )
                )
            args["new_str"] = cached.first_line + "\n" + new_str
            return mt.CallToolRequestParams(name=params.name, arguments=args)

        return params

    def _strip_marker_line(self, content: str) -> str:
        if not isinstance(content, str):
            return content
        first_line, _, rest = content.partition("\n")
        if self._read_emoji in first_line or self._write_emoji in first_line:
            return rest
        return content

    async def _handle_create_pages_request(
        self, params: mt.CallToolRequestParams
    ) -> mt.CallToolRequestParams:
        args = dict(params.arguments or {})
        parent = args.get("parent")

        if parent:
            if isinstance(parent, str):
                try:
                    parent = json.loads(parent)
                    args["parent"] = parent
                except (json.JSONDecodeError, ValueError):
                    pass

            assert isinstance(parent, dict), "Parent must be a dict with a page_id"
            cached = await self._ensure_cached(parent["page_id"], AccessLevel.WRITE)

            new_pages = []
            for page in args.get("pages", []):
                content = self._strip_marker_line(page.get("content", ""))
                page["content"] = cached.first_line + "\n" + content
                new_pages.append(page)
            args["pages"] = new_pages
            return mt.CallToolRequestParams(name=params.name, arguments=args)

        if any(isinstance(page, dict) and "parent" in page for page in args.get("pages", [])):
            raise McpError(
                ErrorData(
                    code=_ERR_ACCESS_DENIED,
                    message=(
                        "Found 'parent' inside page objects, but 'parent' "
                        "must be a top-level argument. Use: "
                        "{parent: {page_id: '...'}, "
                        "pages: [{properties: ..., content: ...}]}"
                    ),
                )
            )
        raise McpError(
            ErrorData(
                code=_ERR_ACCESS_DENIED,
                message=(
                    "Workspace-level page creation is not allowed. "
                    "Specify a top-level parent.page_id argument."
                ),
            )
        )

    def _shorten_image_urls(self, page_id: str, result: ToolResult) -> ToolResult:
        if not result.content:
            return result

        normalized_page_id = _normalize_page_id(page_id)
        new_cache: dict[str, CachedImage] = {}

        def _replace(match: re.Match) -> str:
            alt_text = match.group(1)
            full_url = match.group(2)
            block_id = match.group(3)
            filename = match.group(4)
            new_cache[block_id] = CachedImage(
                filename=filename, alt_text=alt_text, signed_url=full_url
            )
            return f"![{alt_text}](notion-image:{block_id}/{filename})"

        new_content = []
        changed = False
        for block in result.content:
            if isinstance(block, mt.TextContent):
                new_text = _NOTION_S3_IMAGE_RE.sub(_replace, block.text)
                if new_text != block.text:
                    changed = True
                new_content.append(mt.TextContent(type="text", text=new_text))
            else:
                new_content.append(block)

        if not changed:
            self._image_cache.pop(normalized_page_id, None)
            return result

        self._image_cache[normalized_page_id] = new_cache
        return ToolResult(content=new_content)

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

        return self._shorten_image_urls(page_id, result)

    def register_tools(self, server: FastMCP) -> None:  # noqa: F821
        from .image_tools import register_delete_image_tool, register_upload_tool

        if not self._notion_token:
            logging.warning(
                "notion_access: notion_token not configured; "
                "notion-upload-image tool will not be available."
            )
            return
        register_upload_tool(server, self, self._notion_token)
        register_delete_image_tool(server, self, self._notion_token)
