"""Notion content-based access control plugin.

Enforces per-bot, per-page permissions using markers embedded in the first line
of each page's content. Marker format: ``BotName 🖊`` (read-write) or
``BotName 👀`` (read-only), appearing anywhere on the first line.

Also provides a ``notion-upload-image`` synthetic tool (when ``notion_token`` is
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
    from fastmcp import Client, FastMCP

_ERR_ACCESS_DENIED = -32601
_NOTION_API = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"
_PLACEHOLDER_PREFIX = "IMAGE_UPLOAD:"

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

_IMAGE_PLACEHOLDER_RE = re.compile(r"!\[[^\]]*\]\(notion-image:[^)]+\)\n?")

# Tools that don't operate on specific pages
_PASSTHROUGH_TOOLS = {"notion-search", "notion-get-teams", "notion-get-users"}

# Tools that require WRITE access on a page
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


def _strip_image_placeholders(text: str) -> str:
    """Remove notion-image: markdown placeholders from text."""
    return _IMAGE_PLACEHOLDER_RE.sub("", text)


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
        self._image_cache: dict[str, dict[str, CachedImage]] = {}
        self._notion_token = config.notion_token
        self.hide_blocked = config.hide_blocked
        self._client: Client | None = None

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

    def set_upstream_client(self, client: object) -> None:
        from fastmcp import Client

        if isinstance(client, Client):
            self._client = client

    async def _auto_fetch(self, page_id: str) -> None:
        """Fetch page from upstream, parse markers, populate cache."""
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
        """Return cached permission, auto-fetching if not in cache."""
        entry = self._get_cached(page_id)
        if entry is None:
            if self._client is not None:
                await self._auto_fetch(page_id)
                entry = self._get_cached(page_id)
            if entry is None:
                raise McpError(
                    ErrorData(
                        code=_ERR_ACCESS_DENIED,
                        message=f"Page {page_id} not in cache. Fetch the page first to check permissions.",
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
    # Visibility helper
    # ------------------------------------------------------------------

    def is_tool_allowed(self, name: str) -> bool:
        return name not in self._block_tools

    # ------------------------------------------------------------------
    # on_list_tools: remove blocked tools (when hide_blocked is True)
    # ------------------------------------------------------------------

    async def on_list_tools(self, tools: list[Tool]) -> list[Tool]:
        if not self.hide_blocked:
            return tools
        return [t for t in tools if self.is_tool_allowed(t.name)]

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
            await self._ensure_cached(page_id, AccessLevel.READ)
            return params

        # Write tools that operate on a single page_id
        if name in _WRITE_TOOLS:
            page_id = args.get("page_id", "")
            cached = await self._ensure_cached(page_id, AccessLevel.WRITE)

            # For notion-update-page: protect the first line
            if name == "notion-update-page":
                params = self._protect_first_line(params, cached)

            return params

        # notion-move-pages: requires WRITE on all pages being moved
        if name == "notion-move-pages":
            page_ids = args.get("page_or_database_ids", [])
            for pid in page_ids:
                await self._ensure_cached(pid, AccessLevel.WRITE)
            return params

        # notion-create-pages: check parent permission
        if name == "notion-create-pages":
            return await self._handle_create_pages_request(params)

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
            new_updates = []
            updates_changed = False
            for update in content_updates:
                if isinstance(update, dict):
                    old_str = update.get("old_str", "")
                    new_str = update.get("new_str", "")
                else:
                    old_str = getattr(update, "old_str", "")
                    new_str = getattr(update, "new_str", "")
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
                if isinstance(update, dict):
                    stripped = _strip_image_placeholders(new_str)
                    if stripped != new_str:
                        updates_changed = True
                        update = dict(update)
                        update["new_str"] = stripped
                new_updates.append(update)
            if updates_changed:
                args["content_updates"] = new_updates
                return mt.CallToolRequestParams(name=params.name, arguments=args)

        elif command == "replace_content":
            new_str = args.get("new_str", "")
            new_str = _strip_image_placeholders(new_str)
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

    async def _handle_create_pages_request(
        self, params: mt.CallToolRequestParams
    ) -> mt.CallToolRequestParams:
        """Check parent permission for create-pages; inherit first line."""
        args = dict(params.arguments or {})
        parent = args.get("parent")

        # Handle parent passed as a JSON string instead of a dict
        if parent and isinstance(parent, str):
            try:
                parent = json.loads(parent)
                args["parent"] = parent
            except (json.JSONDecodeError, ValueError):
                pass

        if parent and isinstance(parent, dict):
            parent_page_id = parent.get("page_id", "")
            if parent_page_id:
                cached = await self._ensure_cached(parent_page_id, AccessLevel.WRITE)
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
                # Detect parent mistakenly placed inside page objects
                pages = args.get("pages", [])
                has_nested_parent = any(
                    isinstance(p, dict) and "parent" in p for p in pages
                )
                if has_nested_parent:
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
                            "Specify a parent page_id as a top-level argument."
                        ),
                    )
                )

        return params

    # ------------------------------------------------------------------
    # on_call_tool_response: inspect notion-fetch responses
    # ------------------------------------------------------------------

    def _shorten_image_urls(self, page_id: str, result: ToolResult) -> ToolResult:
        """Replace S3 signed image URLs with stable notion-image: placeholders.

        Populates ``_image_cache[page_id]`` as a side-effect (full replacement).
        Returns the original result object unchanged if no images are found.
        """
        if not result.content:
            return result

        normalized_page_id = _normalize_page_id(page_id)
        new_cache: dict[str, CachedImage] = {}

        def _replace(m: re.Match) -> str:
            alt_text = m.group(1)
            full_url = m.group(2)
            block_id = m.group(3)
            filename = m.group(4)
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

    # ------------------------------------------------------------------
    # Synthetic tool registration
    # ------------------------------------------------------------------

    def register_tools(self, server: FastMCP) -> None:  # noqa: F821
        if not self._notion_token:
            logging.warning(
                "notion_access: notion_token not configured; "
                "notion-upload-image tool will not be available."
            )
            return
        _register_upload_tool(server, self, self._notion_token)
        _register_delete_image_tool(server, self, self._notion_token)


def _register_upload_tool(
    server: FastMCP,  # noqa: F821
    plugin: NotionAccessPlugin,
    token: str,
) -> None:
    """Register the notion-upload-image tool on the aggregator server."""

    @server.tool(name="notion-upload-image")
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

        # Require WRITE permission — auto-fetches if not cached.
        await plugin._ensure_cached(page_id, AccessLevel.WRITE)

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
            other_placeholders: list[str] = []
            for block in blocks:
                rich_text = block.get("paragraph", {}).get("rich_text", [])
                full_text = "".join(p.get("plain_text", "") for p in rich_text)
                if placeholder in full_text:
                    placeholder_block_id = block["id"]
                    preceding_block_id = prev_id
                    break
                if f"[{_PLACEHOLDER_PREFIX}" in full_text:
                    other_placeholders.append(full_text.strip())
                prev_id = block["id"]

            if placeholder_block_id is None:
                hint = (
                    f" Found other placeholder(s): {other_placeholders}"
                    " — the file_path must match exactly."
                    if other_placeholders
                    else " No IMAGE_UPLOAD placeholders found on this page."
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


def _register_delete_image_tool(
    server: FastMCP,  # noqa: F821
    plugin: NotionAccessPlugin,
    token: str,
) -> None:
    """Register the notion-delete-image tool on the aggregator server."""

    @server.tool(name="notion-delete-image")
    async def notion_delete_image(page_id: str, block_ids: list[str]) -> str:
        """Delete image blocks from a Notion page.

        Use this tool to remove images before using replace_content on a page
        that contains images. The block IDs come from the
        ``notion-image:BLOCK_ID/FILENAME`` placeholders shown in
        ``notion-fetch`` output.

        Args:
            page_id: ID of the Notion page containing the images.
            block_ids: List of image block IDs to delete.
        """
        page_id = _normalize_page_id(page_id)

        # Require WRITE permission — auto-fetches if not cached.
        await plugin._ensure_cached(page_id, AccessLevel.WRITE)

        json_headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": _NOTION_VERSION,
            "Content-Type": "application/json",
        }

        deleted: list[str] = []
        errors: list[str] = []

        async with httpx.AsyncClient() as client:
            # List the page's children to resolve actual Notion block IDs.
            # The UUID in a notion-image: placeholder is a file-upload UUID
            # embedded in the S3 URL, NOT the Notion block ID — so we match
            # via the cached signed URL (base path without query params).
            list_resp = await client.get(
                f"{_NOTION_API}/blocks/{page_id}/children",
                headers=json_headers,
            )
            url_to_notion_block_id: dict[str, str] = {}
            if list_resp.is_success:
                for block in list_resp.json().get("results", []):
                    if block.get("type") == "image":
                        url = block.get("image", {}).get("file", {}).get("url", "")
                        if url:
                            url_to_notion_block_id[url.split("?")[0]] = block["id"]

            for raw_block_id in block_ids:
                # Normalize: strip "notion-image:" prefix and "/filename" suffix.
                block_id = raw_block_id.removeprefix("notion-image:").split("/")[0]

                # Resolve to the actual Notion block ID via the image cache.
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
                        f"{_NOTION_API}/blocks/{actual_id}",
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
