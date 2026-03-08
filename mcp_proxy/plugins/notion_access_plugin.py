"""Notion content-based access control plugin.

Enforces per-bot, per-page permissions using markers embedded in the first line
of each page's content. Marker format: ``BotName 🖊`` (read-write) or
``BotName 👀`` (read-only), appearing anywhere on the first line.
"""

from __future__ import annotations

import enum
import re
import time
from dataclasses import dataclass

import mcp.types as mt
from fastmcp.tools.tool import Tool, ToolResult
from mcp import McpError
from mcp.types import ErrorData

from ..config.schema import NotionAccessPluginConfig
from .base import PluginBase

_ERR_ACCESS_DENIED = -32601

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


def _parse_permission(
    content: str,
    bot_name: str,
    read_emoji: str,
    write_emoji: str,
) -> tuple[AccessLevel, str]:
    """Parse the first line of page content for bot permission markers.

    Returns (access_level, first_line_text).
    """
    # Notion fetch responses wrap content in <content>...</content>
    # Extract first line after <content>\n
    content_match = re.search(r"<content>\n(.*?)(?:\n|$)", content)
    if content_match:
        first_line = content_match.group(1)
    else:
        # Fallback: just use the first line of content
        first_line = content.split("\n")[0] if content else ""

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

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _get_cached(self, page_id: str) -> CachedPermission | None:
        """Return cached permission if present and not expired."""
        entry = self._cache.get(page_id)
        if entry is None:
            return None
        if time.monotonic() > entry.expires_at:
            del self._cache[page_id]
            return None
        return entry

    def _set_cached(self, page_id: str, level: AccessLevel, first_line: str) -> None:
        self._cache[page_id] = CachedPermission(
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
                old_str = update.get("old_str", "")
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
                    content = page.get("content", "")
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
