"""Tests for the Notion content-based access control plugin."""

from __future__ import annotations

import mcp.types as mt
import pytest
from fastmcp.tools.tool import ToolResult
from mcp import McpError

from mcp_proxy.config.schema import NotionAccessPluginConfig
from mcp_proxy.plugins.notion_access_plugin import (
    _NOTION_S3_IMAGE_RE,
    AccessLevel,
    NotionAccessPlugin,
    _extract_text,
    _normalize_page_id,
    _parse_permission,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BOT = "OcelliBot"
READ_EMOJI = "👀"
WRITE_EMOJI = "🖊"


def _cfg(**overrides) -> NotionAccessPluginConfig:
    defaults = dict(type="notion_access", bot_name=BOT)
    defaults.update(overrides)
    return NotionAccessPluginConfig(**defaults)


def _plugin(**overrides) -> NotionAccessPlugin:
    return NotionAccessPlugin(_cfg(**overrides))


def make_call(name: str, arguments: dict | None = None) -> mt.CallToolRequestParams:
    return mt.CallToolRequestParams(name=name, arguments=arguments)


def make_result(text: str) -> ToolResult:
    return ToolResult(content=[mt.TextContent(type="text", text=text)])


def _notion_page(first_line: str, body: str = "Some body text.") -> str:
    """Build a fake notion-fetch response with <content> wrapper."""
    return (
        'Here is the result of "view" for the Page...\n'
        '<page url="https://www.notion.so/abc123">\n'
        "<properties>\n"
        '{"title":"Test Page"}\n'
        "</properties>\n"
        "<content>\n"
        f"{first_line}\n"
        f"{body}\n"
        "</content>\n"
        "</page>"
    )


# ---------------------------------------------------------------------------
# Permission parsing
# ---------------------------------------------------------------------------


class TestParsePermission:
    def test_write_marker(self):
        content = _notion_page(f"{BOT} {WRITE_EMOJI}, AnotherBot {READ_EMOJI}")
        level, first_line = _parse_permission(content, BOT, READ_EMOJI, WRITE_EMOJI)
        assert level == AccessLevel.WRITE
        assert BOT in first_line

    def test_read_marker(self):
        content = _notion_page(f"{BOT} {READ_EMOJI}")
        level, _ = _parse_permission(content, BOT, READ_EMOJI, WRITE_EMOJI)
        assert level == AccessLevel.READ

    def test_no_marker(self):
        content = _notion_page("Just some text with no markers")
        level, _ = _parse_permission(content, BOT, READ_EMOJI, WRITE_EMOJI)
        assert level == AccessLevel.NONE

    def test_both_emojis_highest_wins(self):
        content = _notion_page(f"{BOT} {READ_EMOJI}, {BOT} {WRITE_EMOJI}")
        level, _ = _parse_permission(content, BOT, READ_EMOJI, WRITE_EMOJI)
        assert level == AccessLevel.WRITE

    def test_different_bot_no_match(self):
        content = _notion_page(f"OtherBot {WRITE_EMOJI}")
        level, _ = _parse_permission(content, BOT, READ_EMOJI, WRITE_EMOJI)
        assert level == AccessLevel.NONE

    def test_multiple_bots(self):
        content = _notion_page(
            f"AlphaBot {WRITE_EMOJI}, {BOT} {READ_EMOJI}, GammaBot {WRITE_EMOJI}"
        )
        level, _ = _parse_permission(content, BOT, READ_EMOJI, WRITE_EMOJI)
        assert level == AccessLevel.READ

    def test_fallback_no_content_tag(self):
        raw = f"{BOT} {WRITE_EMOJI}\nBody text"
        level, first_line = _parse_permission(raw, BOT, READ_EMOJI, WRITE_EMOJI)
        assert level == AccessLevel.WRITE
        assert first_line == f"{BOT} {WRITE_EMOJI}"


# ---------------------------------------------------------------------------
# Fetch pass-through + block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_with_write_marker_passes():
    plugin = _plugin()
    params = make_call("notion-fetch", {"id": "abc123"})
    params = await plugin.on_call_tool_request(params)
    result = make_result(_notion_page(f"{BOT} {WRITE_EMOJI}"))
    out = await plugin.on_call_tool_response(params, result)
    # Result should pass through unchanged
    assert out is result


@pytest.mark.asyncio
async def test_fetch_with_read_marker_passes():
    plugin = _plugin()
    params = make_call("notion-fetch", {"id": "abc123"})
    params = await plugin.on_call_tool_request(params)
    result = make_result(_notion_page(f"{BOT} {READ_EMOJI}"))
    out = await plugin.on_call_tool_response(params, result)
    assert out is result


@pytest.mark.asyncio
async def test_fetch_without_marker_blocked():
    plugin = _plugin()
    params = make_call("notion-fetch", {"id": "abc123"})
    params = await plugin.on_call_tool_request(params)
    result = make_result(_notion_page("No markers here"))
    out = await plugin.on_call_tool_response(params, result)
    # Should be replaced with an error result
    assert out is not result
    assert "[ACCESS DENIED]" in out.content[0].text
    assert BOT in out.content[0].text


# ---------------------------------------------------------------------------
# Cache TTL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_ttl_expiry():
    plugin = _plugin(cache_ttl_seconds=0)  # immediate expiry
    # Fetch to populate cache
    params = make_call("notion-fetch", {"id": "page1"})
    result = make_result(_notion_page(f"{BOT} {WRITE_EMOJI}"))
    await plugin.on_call_tool_response(params, result)

    # Cache should have expired immediately (TTL=0)
    # Try to write — should fail because cache expired
    write_params = make_call(
        "notion-update-page", {"page_id": "page1", "command": "update_properties"}
    )
    with pytest.raises(McpError) as exc:
        await plugin.on_call_tool_request(write_params)
    assert "not in cache" in str(exc.value).lower() or "cache" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Write blocked without cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_blocked_without_prior_fetch():
    plugin = _plugin()
    params = make_call(
        "notion-update-page", {"page_id": "unknown_page", "command": "update_properties"}
    )
    with pytest.raises(McpError) as exc:
        await plugin.on_call_tool_request(params)
    assert "cache" in str(exc.value).lower() or "fetch" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Write blocked with read-only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_blocked_with_read_only():
    plugin = _plugin()
    # Fetch with read-only marker
    fetch_params = make_call("notion-fetch", {"id": "page1"})
    result = make_result(_notion_page(f"{BOT} {READ_EMOJI}"))
    await plugin.on_call_tool_response(fetch_params, result)

    # Attempt write
    write_params = make_call(
        "notion-update-page", {"page_id": "page1", "command": "update_properties"}
    )
    with pytest.raises(McpError) as exc:
        await plugin.on_call_tool_request(write_params)
    assert "write" in str(exc.value).lower() or "permission" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Write allowed with read-write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_allowed_with_rw():
    plugin = _plugin()
    # Fetch with write marker
    fetch_params = make_call("notion-fetch", {"id": "page1"})
    result = make_result(_notion_page(f"{BOT} {WRITE_EMOJI}"))
    await plugin.on_call_tool_response(fetch_params, result)

    # Write should pass
    write_params = make_call(
        "notion-update-page",
        {"page_id": "page1", "command": "update_properties", "properties": {"title": "New"}},
    )
    out = await plugin.on_call_tool_request(write_params)
    assert out.name == "notion-update-page"


# ---------------------------------------------------------------------------
# First line protection (update_content)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_content_targeting_first_line_blocked():
    plugin = _plugin()
    first_line = f"{BOT} {WRITE_EMOJI}, AdminBot {WRITE_EMOJI}"
    fetch_params = make_call("notion-fetch", {"id": "page1"})
    result = make_result(_notion_page(first_line))
    await plugin.on_call_tool_response(fetch_params, result)

    # Try to edit the first line via update_content
    update_params = make_call(
        "notion-update-page",
        {
            "page_id": "page1",
            "command": "update_content",
            "content_updates": [{"old_str": first_line, "new_str": "replaced!"}],
        },
    )
    with pytest.raises(McpError) as exc:
        await plugin.on_call_tool_request(update_params)
    assert "marker" in str(exc.value).lower() or "first line" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_update_content_not_targeting_first_line_passes():
    plugin = _plugin()
    first_line = f"{BOT} {WRITE_EMOJI}"
    fetch_params = make_call("notion-fetch", {"id": "page1"})
    result = make_result(_notion_page(first_line, body="Old body text."))
    await plugin.on_call_tool_response(fetch_params, result)

    # Edit body content (not the first line)
    update_params = make_call(
        "notion-update-page",
        {
            "page_id": "page1",
            "command": "update_content",
            "content_updates": [{"old_str": "Old body text.", "new_str": "New body text."}],
        },
    )
    out = await plugin.on_call_tool_request(update_params)
    assert out.name == "notion-update-page"


# ---------------------------------------------------------------------------
# replace_content first line preservation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replace_content_prepends_first_line():
    plugin = _plugin()
    first_line = f"{BOT} {WRITE_EMOJI}, AdminBot {WRITE_EMOJI}"
    fetch_params = make_call("notion-fetch", {"id": "page1"})
    result = make_result(_notion_page(first_line))
    await plugin.on_call_tool_response(fetch_params, result)

    replace_params = make_call(
        "notion-update-page",
        {"page_id": "page1", "command": "replace_content", "new_str": "Brand new content."},
    )
    out = await plugin.on_call_tool_request(replace_params)
    # The first line should be prepended
    assert out.arguments["new_str"].startswith(first_line + "\n")
    assert "Brand new content." in out.arguments["new_str"]


# ---------------------------------------------------------------------------
# create-pages inheritance
# ---------------------------------------------------------------------------


def _create_page(title: str, content: str = "") -> dict:
    """Build a page dict (no parent — parent is top-level in the API)."""
    page: dict = {"properties": {"title": title}}
    if content:
        page["content"] = content
    return page


@pytest.mark.asyncio
async def test_create_pages_inherits_first_line():
    plugin = _plugin()
    first_line = f"{BOT} {WRITE_EMOJI}"
    fetch_params = make_call("notion-fetch", {"id": "parent1"})
    await plugin.on_call_tool_response(fetch_params, make_result(_notion_page(first_line)))

    create_params = make_call(
        "notion-create-pages",
        {"parent": {"page_id": "parent1"}, "pages": [_create_page("Child", "Child body.")]},
    )
    out = await plugin.on_call_tool_request(create_params)
    page_content = out.arguments["pages"][0]["content"]
    assert page_content.startswith(first_line + "\n")
    assert "Child body." in page_content


@pytest.mark.asyncio
async def test_create_pages_strips_duplicate_marker():
    """If the LLM already included a marker line, the proxy strips it and uses the parent's."""
    plugin = _plugin()
    first_line = f"{BOT} {WRITE_EMOJI}"
    fetch_params = make_call("notion-fetch", {"id": "parent1"})
    await plugin.on_call_tool_response(fetch_params, make_result(_notion_page(first_line)))

    create_params = make_call(
        "notion-create-pages",
        {
            "parent": {"page_id": "parent1"},
            "pages": [_create_page("Child", f"{BOT} {WRITE_EMOJI}\nChild body.")],
        },
    )
    out = await plugin.on_call_tool_request(create_params)
    page_content = out.arguments["pages"][0]["content"]
    assert page_content == f"{first_line}\nChild body."


@pytest.mark.asyncio
async def test_create_pages_strips_llm_marker_uses_parent():
    """If the LLM provides a different marker, the proxy replaces it with the parent's."""
    plugin = _plugin()
    parent_line = f"{BOT} {READ_EMOJI}, AdminBot {WRITE_EMOJI}"
    fetch_params = make_call("notion-fetch", {"id": "parent1"})
    await plugin.on_call_tool_response(fetch_params, make_result(_notion_page(parent_line)))

    plugin._cache["parent1"].level = AccessLevel.WRITE

    create_params = make_call(
        "notion-create-pages",
        {
            "parent": {"page_id": "parent1"},
            "pages": [_create_page("Child", f"{BOT} {WRITE_EMOJI}\nChild body.")],
        },
    )
    out = await plugin.on_call_tool_request(create_params)
    page_content = out.arguments["pages"][0]["content"]
    assert page_content.startswith(parent_line + "\n")
    assert "Child body." in page_content


@pytest.mark.asyncio
async def test_create_pages_no_content():
    """Pages with no content still get the marker line."""
    plugin = _plugin()
    first_line = f"{BOT} {WRITE_EMOJI}"
    fetch_params = make_call("notion-fetch", {"id": "parent1"})
    await plugin.on_call_tool_response(fetch_params, make_result(_notion_page(first_line)))

    create_params = make_call(
        "notion-create-pages",
        {"parent": {"page_id": "parent1"}, "pages": [_create_page("Empty Child")]},
    )
    out = await plugin.on_call_tool_request(create_params)
    page_content = out.arguments["pages"][0]["content"]
    assert page_content == f"{first_line}\n"


@pytest.mark.asyncio
async def test_create_pages_without_parent_blocked():
    plugin = _plugin()
    create_params = make_call(
        "notion-create-pages",
        {"pages": [{"properties": {"title": "Root"}, "content": "stuff"}]},
    )
    with pytest.raises(McpError) as exc:
        await plugin.on_call_tool_request(create_params)
    assert "workspace" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_create_pages_nested_parent_gives_helpful_error():
    """Parent inside page objects (wrong format) gives a specific error message."""
    plugin = _plugin()
    create_params = make_call(
        "notion-create-pages",
        {"pages": [{"parent": {"page_id": "p1"}, "properties": {"title": "X"}}]},
    )
    with pytest.raises(McpError) as exc:
        await plugin.on_call_tool_request(create_params)
    assert "top-level" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_create_pages_string_parent_decoded():
    """Parent passed as a JSON string is decoded and works normally."""
    plugin = _plugin()
    first_line = f"{BOT} {WRITE_EMOJI}"
    fetch_params = make_call("notion-fetch", {"id": "parent1"})
    await plugin.on_call_tool_response(fetch_params, make_result(_notion_page(first_line)))

    create_params = make_call(
        "notion-create-pages",
        {
            "parent": '{"page_id": "parent1"}',
            "pages": [_create_page("Child", "Body text.")],
        },
    )
    out = await plugin.on_call_tool_request(create_params)
    page_content = out.arguments["pages"][0]["content"]
    assert page_content.startswith(first_line + "\n")
    assert "Body text." in page_content


# ---------------------------------------------------------------------------
# Blocked tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_tool_raises():
    plugin = _plugin()
    params = make_call("notion-create-database", {"parent": {"page_id": "x"}})
    with pytest.raises(McpError) as exc:
        await plugin.on_call_tool_request(params)
    assert "blocked" in str(exc.value).lower()


def test_blocked_tool_policy_declared():
    plugin = _plugin()
    assert plugin.hide_blocked is True
    assert plugin.is_tool_allowed("notion-search") is True
    assert plugin.is_tool_allowed("notion-create-database") is False
    assert plugin.is_tool_allowed("notion-update-data-source") is False


# ---------------------------------------------------------------------------
# Allowed passthrough tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_passthrough_tools_allowed():
    plugin = _plugin()
    for tool_name in ["notion-search", "notion-get-teams", "notion-get-users"]:
        params = make_call(tool_name, {})
        out = await plugin.on_call_tool_request(params)
        assert out is params


# ---------------------------------------------------------------------------
# notion-get-comments requires READ
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_comments_requires_read():
    plugin = _plugin()
    # No cache — should fail
    params = make_call("notion-get-comments", {"page_id": "page1"})
    with pytest.raises(McpError):
        await plugin.on_call_tool_request(params)


@pytest.mark.asyncio
async def test_get_comments_passes_with_read():
    plugin = _plugin()
    # Populate cache with read access
    fetch_params = make_call("notion-fetch", {"id": "page1"})
    result = make_result(_notion_page(f"{BOT} {READ_EMOJI}"))
    await plugin.on_call_tool_response(fetch_params, result)

    params = make_call("notion-get-comments", {"page_id": "page1"})
    out = await plugin.on_call_tool_request(params)
    assert out is params


# ---------------------------------------------------------------------------
# notion-move-pages requires WRITE on all pages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_move_pages_requires_write_on_all():
    plugin = _plugin()
    # Cache page1 as WRITE, page2 as READ
    for pid, emoji in [("page1", WRITE_EMOJI), ("page2", READ_EMOJI)]:
        fetch_params = make_call("notion-fetch", {"id": pid})
        result = make_result(_notion_page(f"{BOT} {emoji}"))
        await plugin.on_call_tool_response(fetch_params, result)

    params = make_call(
        "notion-move-pages",
        {"page_or_database_ids": ["page1", "page2"], "new_parent": {"page_id": "dest"}},
    )
    with pytest.raises(McpError):
        await plugin.on_call_tool_request(params)


@pytest.mark.asyncio
async def test_move_pages_passes_with_write():
    plugin = _plugin()
    for pid in ["page1", "page2"]:
        fetch_params = make_call("notion-fetch", {"id": pid})
        result = make_result(_notion_page(f"{BOT} {WRITE_EMOJI}"))
        await plugin.on_call_tool_response(fetch_params, result)

    params = make_call(
        "notion-move-pages",
        {"page_or_database_ids": ["page1", "page2"], "new_parent": {"page_id": "dest"}},
    )
    out = await plugin.on_call_tool_request(params)
    assert out is params


# ---------------------------------------------------------------------------
# notion-duplicate-page requires WRITE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_page_requires_write():
    plugin = _plugin()
    fetch_params = make_call("notion-fetch", {"id": "page1"})
    result = make_result(_notion_page(f"{BOT} {READ_EMOJI}"))
    await plugin.on_call_tool_response(fetch_params, result)

    params = make_call("notion-duplicate-page", {"page_id": "page1"})
    with pytest.raises(McpError):
        await plugin.on_call_tool_request(params)


# ---------------------------------------------------------------------------
# notion-create-comment requires WRITE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_comment_requires_write():
    plugin = _plugin()
    fetch_params = make_call("notion-fetch", {"id": "page1"})
    result = make_result(_notion_page(f"{BOT} {READ_EMOJI}"))
    await plugin.on_call_tool_response(fetch_params, result)

    params = make_call("notion-create-comment", {"page_id": "page1", "rich_text": []})
    with pytest.raises(McpError):
        await plugin.on_call_tool_request(params)


@pytest.mark.asyncio
async def test_create_comment_passes_with_write():
    plugin = _plugin()
    fetch_params = make_call("notion-fetch", {"id": "page1"})
    result = make_result(_notion_page(f"{BOT} {WRITE_EMOJI}"))
    await plugin.on_call_tool_response(fetch_params, result)

    params = make_call("notion-create-comment", {"page_id": "page1", "rich_text": []})
    out = await plugin.on_call_tool_request(params)
    assert out is params


# ---------------------------------------------------------------------------
# Auto-fetch on permission miss
# ---------------------------------------------------------------------------


class _MockClient:
    """Minimal upstream client mock for auto-fetch tests."""

    def __init__(self, response_text: str, raises: bool = False) -> None:
        self._text = response_text
        self._raises = raises
        self.call_count = 0

    async def call_tool(self, name: str, args: dict) -> ToolResult:
        self.call_count += 1
        if self._raises:
            raise RuntimeError("upstream connection failed")
        return make_result(self._text)


@pytest.mark.asyncio
async def test_auto_fetch_allows_write():
    """Write on uncached page succeeds when auto-fetch returns a WRITE marker."""
    plugin = _plugin()
    plugin._client = _MockClient(_notion_page(f"{BOT} {WRITE_EMOJI}"))

    params = make_call(
        "notion-update-page",
        {"page_id": "page1", "command": "update_properties", "properties": {"title": "New"}},
    )
    out = await plugin.on_call_tool_request(params)
    assert out.name == "notion-update-page"
    assert plugin._client.call_count == 1  # auto-fetch was called


@pytest.mark.asyncio
async def test_auto_fetch_read_only_blocks_write():
    """Auto-fetch returns READ marker — write must be blocked."""
    plugin = _plugin()
    plugin._client = _MockClient(_notion_page(f"{BOT} {READ_EMOJI}"))

    params = make_call("notion-update-page", {"page_id": "page1", "command": "update_properties"})
    with pytest.raises(McpError) as exc:
        await plugin.on_call_tool_request(params)
    assert "write" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_auto_fetch_no_marker_blocks():
    """Auto-fetch returns page with no marker — must be blocked."""
    plugin = _plugin()
    plugin._client = _MockClient(_notion_page("No markers here"))

    params = make_call("notion-update-page", {"page_id": "page1", "command": "update_properties"})
    with pytest.raises(McpError) as exc:
        await plugin.on_call_tool_request(params)
    assert "permission marker" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_auto_fetch_failure_falls_back_to_cache_error():
    """If auto-fetch raises, fall back to the 'not in cache' error."""
    plugin = _plugin()
    plugin._client = _MockClient("", raises=True)

    params = make_call("notion-update-page", {"page_id": "page1", "command": "update_properties"})
    with pytest.raises(McpError) as exc:
        await plugin.on_call_tool_request(params)
    assert "cache" in str(exc.value).lower() or "fetch" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_auto_fetch_skipped_on_cache_hit():
    """If page is already cached, the client must not be called."""
    plugin = _plugin()
    # Populate cache manually via the fetch response hook
    fetch_params = make_call("notion-fetch", {"id": "page1"})
    result = make_result(_notion_page(f"{BOT} {WRITE_EMOJI}"))
    await plugin.on_call_tool_response(fetch_params, result)

    mock = _MockClient(_notion_page(f"{BOT} {WRITE_EMOJI}"))
    plugin._client = mock

    params = make_call(
        "notion-update-page",
        {"page_id": "page1", "command": "update_properties", "properties": {"title": "New"}},
    )
    await plugin.on_call_tool_request(params)
    assert mock.call_count == 0  # cache was hit, no auto-fetch


# ---------------------------------------------------------------------------
# S3 image URL regex
# ---------------------------------------------------------------------------

_S3_BASE = "https://prod-files-secure.s3.us-west-2.amazonaws.com"
_WS_ID = "aabbccdd001122334455667788990011"
_BLOCK_ID_1 = "deadbeef123456789abcdef012345678"
_BLOCK_ID_2 = "cafebabe123456789abcdef012345678"
_FILENAME_1 = "photo.jpg"
_FILENAME_2 = "diagram.png"


def _s3_url(block_id: str = _BLOCK_ID_1, filename: str = _FILENAME_1) -> str:
    return f"{_S3_BASE}/{_WS_ID}/{block_id}/{filename}?X-Amz-Security-Token=abc&expires=9999"


def _image_md(alt: str, block_id: str = _BLOCK_ID_1, filename: str = _FILENAME_1) -> str:
    return f"![{alt}]({_s3_url(block_id, filename)})"


def _placeholder(alt: str, block_id: str = _BLOCK_ID_1, filename: str = _FILENAME_1) -> str:
    return f"![{alt}](notion-image:{block_id}/{filename})"


class TestNotionS3ImageRegex:
    def test_s3_url_matched(self):
        text = _image_md("alt text")
        m = _NOTION_S3_IMAGE_RE.search(text)
        assert m is not None
        assert m.group(1) == "alt text"
        assert m.group(3) == _BLOCK_ID_1
        assert m.group(4) == _FILENAME_1

    def test_non_s3_url_not_matched(self):
        text = "![image](https://example.com/image.jpg)"
        assert _NOTION_S3_IMAGE_RE.search(text) is None

    def test_empty_alt_text_matched(self):
        text = _image_md("")
        m = _NOTION_S3_IMAGE_RE.search(text)
        assert m is not None
        assert m.group(1) == ""

    def test_alt_text_with_special_chars(self):
        text = _image_md("a chart: 50% growth")
        m = _NOTION_S3_IMAGE_RE.search(text)
        assert m is not None
        assert m.group(1) == "a chart: 50% growth"

    def test_full_url_in_group2(self):
        url = _s3_url()
        text = f"![x]({url})"
        m = _NOTION_S3_IMAGE_RE.search(text)
        assert m is not None
        assert m.group(2) == url


# ---------------------------------------------------------------------------
# Image URL shortening (on_call_tool_response)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_with_images_shortened():
    plugin = _plugin()
    img = _image_md("Photo")
    page_content = _notion_page(f"{BOT} {WRITE_EMOJI}", body=img)
    params = make_call("notion-fetch", {"id": "page1"})
    result = make_result(page_content)
    out = await plugin.on_call_tool_response(params, result)
    text = _extract_text(out)
    assert f"notion-image:{_BLOCK_ID_1}/{_FILENAME_1}" in text
    assert _s3_url() not in text


@pytest.mark.asyncio
async def test_image_cache_populated_after_fetch():
    plugin = _plugin()
    img = _image_md("Photo")
    page_content = _notion_page(f"{BOT} {WRITE_EMOJI}", body=img)
    params = make_call("notion-fetch", {"id": "page1"})
    await plugin.on_call_tool_response(params, make_result(page_content))
    normalized = _normalize_page_id("page1")
    assert normalized in plugin._image_cache
    assert _BLOCK_ID_1 in plugin._image_cache[normalized]
    assert plugin._image_cache[normalized][_BLOCK_ID_1].filename == _FILENAME_1


@pytest.mark.asyncio
async def test_no_images_returns_same_result_object():
    plugin = _plugin()
    page_content = _notion_page(f"{BOT} {WRITE_EMOJI}", body="No images here.")
    params = make_call("notion-fetch", {"id": "page1"})
    result = make_result(page_content)
    out = await plugin.on_call_tool_response(params, result)
    assert out is result


@pytest.mark.asyncio
async def test_multiple_images_all_shortened():
    plugin = _plugin()
    img1 = _image_md("A", _BLOCK_ID_1, _FILENAME_1)
    img2 = _image_md("B", _BLOCK_ID_2, _FILENAME_2)
    page_content = _notion_page(f"{BOT} {WRITE_EMOJI}", body=f"{img1}\n{img2}")
    params = make_call("notion-fetch", {"id": "page1"})
    out = await plugin.on_call_tool_response(params, make_result(page_content))
    text = _extract_text(out)
    assert f"notion-image:{_BLOCK_ID_1}/{_FILENAME_1}" in text
    assert f"notion-image:{_BLOCK_ID_2}/{_FILENAME_2}" in text
    assert _s3_url(_BLOCK_ID_1) not in text
    assert _s3_url(_BLOCK_ID_2) not in text


@pytest.mark.asyncio
async def test_refetch_replaces_image_cache_atomically():
    plugin = _plugin()
    params = make_call("notion-fetch", {"id": "page1"})

    img1 = _image_md("A", _BLOCK_ID_1, _FILENAME_1)
    await plugin.on_call_tool_response(
        params, make_result(_notion_page(f"{BOT} {WRITE_EMOJI}", body=img1))
    )

    img2 = _image_md("B", _BLOCK_ID_2, _FILENAME_2)
    await plugin.on_call_tool_response(
        params, make_result(_notion_page(f"{BOT} {WRITE_EMOJI}", body=img2))
    )

    normalized = _normalize_page_id("page1")
    assert _BLOCK_ID_1 not in plugin._image_cache.get(normalized, {})
    assert _BLOCK_ID_2 in plugin._image_cache.get(normalized, {})


@pytest.mark.asyncio
async def test_refetch_without_images_clears_image_cache():
    plugin = _plugin()
    params = make_call("notion-fetch", {"id": "page1"})

    await plugin.on_call_tool_response(
        params,
        make_result(_notion_page(f"{BOT} {WRITE_EMOJI}", body=_image_md("A"))),
    )

    assert _normalize_page_id("page1") in plugin._image_cache

    out = await plugin.on_call_tool_response(
        params,
        make_result(_notion_page(f"{BOT} {WRITE_EMOJI}", body="No images remain.")),
    )

    assert out.content[0].text.endswith("No images remain.\n</content>\n</page>")
    assert _normalize_page_id("page1") not in plugin._image_cache


# ---------------------------------------------------------------------------
# Image placeholder edits are rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replace_content_with_images_blocked():
    plugin = _plugin()
    fetch_params = make_call("notion-fetch", {"id": "page1"})
    first_line = f"{BOT} {WRITE_EMOJI}"
    await plugin.on_call_tool_response(
        fetch_params,
        make_result(_notion_page(first_line, body=_image_md("Photo"))),
    )

    replace_params = make_call(
        "notion-update-page",
        {
            "page_id": "page1",
            "command": "replace_content",
            "new_str": "Some text\nMore text",
        },
    )
    with pytest.raises(McpError) as exc:
        await plugin.on_call_tool_request(replace_params)
    assert "notion-delete-image" in str(exc.value)


@pytest.mark.asyncio
async def test_replace_content_with_placeholder_rejected():
    plugin = _plugin()
    fetch_params = make_call("notion-fetch", {"id": "page1"})
    first_line = f"{BOT} {WRITE_EMOJI}"
    await plugin.on_call_tool_response(fetch_params, make_result(_notion_page(first_line)))

    ph = _placeholder("Photo")
    replace_params = make_call(
        "notion-update-page",
        {
            "page_id": "page1",
            "command": "replace_content",
            "new_str": f"new body\n{ph}",
        },
    )
    with pytest.raises(McpError) as exc:
        await plugin.on_call_tool_request(replace_params)
    assert "notion-image" in str(exc.value)


@pytest.mark.asyncio
async def test_update_content_new_placeholder_rejected():
    plugin = _plugin()
    fetch_params = make_call("notion-fetch", {"id": "page1"})
    first_line = f"{BOT} {WRITE_EMOJI}"
    await plugin.on_call_tool_response(fetch_params, make_result(_notion_page(first_line)))

    ph = _placeholder("Photo")
    update_params = make_call(
        "notion-update-page",
        {
            "page_id": "page1",
            "command": "update_content",
            "content_updates": [{"old_str": "body text", "new_str": f"new body\n{ph}"}],
        },
    )
    with pytest.raises(McpError) as exc:
        await plugin.on_call_tool_request(update_params)
    assert "notion-image" in str(exc.value)


@pytest.mark.asyncio
async def test_update_content_old_placeholder_rejected():
    plugin = _plugin()
    fetch_params = make_call("notion-fetch", {"id": "page1"})
    first_line = f"{BOT} {WRITE_EMOJI}"
    await plugin.on_call_tool_response(fetch_params, make_result(_notion_page(first_line)))

    ph = _placeholder("Photo")
    update_params = make_call(
        "notion-update-page",
        {
            "page_id": "page1",
            "command": "update_content",
            "content_updates": [{"old_str": f"body {ph}", "new_str": "new body"}],
        },
    )
    with pytest.raises(McpError) as exc:
        await plugin.on_call_tool_request(update_params)
    assert "notion-image" in str(exc.value)


# ---------------------------------------------------------------------------
# notion-delete-image permission checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_image_requires_write_no_cache():
    """_ensure_cached raises when page has never been fetched."""
    plugin = _plugin()
    with pytest.raises(McpError) as exc:
        await plugin._ensure_cached("page1", AccessLevel.WRITE)
    assert "cache" in str(exc.value).lower() or "fetch" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_delete_image_blocked_with_read_only():
    """_ensure_cached raises WRITE requirement when page is READ-only."""
    plugin = _plugin()
    fetch_params = make_call("notion-fetch", {"id": "page1"})
    await plugin.on_call_tool_response(
        fetch_params, make_result(_notion_page(f"{BOT} {READ_EMOJI}"))
    )

    with pytest.raises(McpError) as exc:
        await plugin._ensure_cached("page1", AccessLevel.WRITE)
    assert "write" in str(exc.value).lower() or "permission" in str(exc.value).lower()


def test_delete_image_block_id_normalization():
    """Block IDs passed as full placeholders or with prefix are stripped correctly."""
    UUID = "b87dd20f-f739-4028-9bf8-37ba747a2897"

    def normalize(raw: str) -> str:
        return raw.removeprefix("notion-image:").split("/")[0]

    assert normalize(UUID) == UUID
    assert normalize(f"notion-image:{UUID}/agentic.png") == UUID
    assert normalize(f"notion-image:{UUID}") == UUID


@pytest.mark.asyncio
async def test_create_pages_requires_write():
    """create-pages is blocked with read-only permission."""
    plugin = _plugin()
    fetch_params = make_call("notion-fetch", {"id": "parent1"})
    await plugin.on_call_tool_response(
        fetch_params, make_result(_notion_page(f"{BOT} {READ_EMOJI}"))
    )

    create_params = make_call(
        "notion-create-pages",
        {"parent": {"page_id": "parent1"}, "pages": [_create_page("Child", "Child body.")]},
    )
    with pytest.raises(McpError):
        await plugin.on_call_tool_request(create_params)
