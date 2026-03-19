"""Tests for plugin infrastructure and built-in plugins."""

from __future__ import annotations

import json

import mcp.types as mt
import pytest
from fastmcp.prompts.prompt import Prompt
from fastmcp.resources.resource import Resource
from fastmcp.tools.tool import Tool, ToolResult
from mcp import McpError
from pydantic import AnyUrl

from mcp_proxy.config.schema import (
    FilterPluginConfig,
    InventoryPluginConfig,
    LoggingPluginConfig,
    RewritePluginConfig,
)
from mcp_proxy.plugins.base import PluginBase
from mcp_proxy.plugins.filter_plugin import FilterPlugin
from mcp_proxy.plugins.inventory_plugin import InventoryPlugin
from mcp_proxy.plugins.logging_plugin import JsonlLoggingPlugin
from mcp_proxy.plugins.rewrite_plugin import RewritePlugin

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_call_params(name: str, arguments: dict | None = None) -> mt.CallToolRequestParams:
    return mt.CallToolRequestParams(name=name, arguments=arguments)


def make_tool_result(text: str) -> ToolResult:
    return ToolResult(content=[mt.TextContent(type="text", text=text)])


def make_tool(name: str) -> Tool:
    return Tool(name=name, description="test", parameters={"type": "object", "properties": {}})


def make_resource(uri: str) -> Resource:
    return Resource(uri=AnyUrl(uri), name="test")


def make_prompt(name: str) -> Prompt:
    return Prompt(name=name, description="test")


# ---------------------------------------------------------------------------
# PluginBase pass-through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plugin_base_passthrough():
    plugin = PluginBase()
    params = make_call_params("my_tool", {"x": 1})
    result = await plugin.on_call_tool_request(params)
    assert result is params

    tool_result = make_tool_result("hello")
    result2 = await plugin.on_call_tool_response(params, tool_result)
    assert result2 is tool_result

    tools = [make_tool("foo")]
    result3 = await plugin.on_list_tools(tools)
    assert result3 is tools


# ---------------------------------------------------------------------------
# FilterPlugin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_block_tools_list():
    plugin = FilterPlugin(FilterPluginConfig(type="filter", block_tools=["write_*", "delete_*"]))
    tools = [make_tool("read_file"), make_tool("write_file"), make_tool("delete_dir")]
    result = await plugin.on_list_tools(tools)
    assert [t.name for t in result] == ["read_file"]


@pytest.mark.asyncio
async def test_filter_allow_tools_list():
    plugin = FilterPlugin(FilterPluginConfig(type="filter", allow_tools=["read_*"]))
    tools = [make_tool("read_file"), make_tool("write_file"), make_tool("read_dir")]
    result = await plugin.on_list_tools(tools)
    assert [t.name for t in result] == ["read_file", "read_dir"]


@pytest.mark.asyncio
async def test_filter_block_tool_call_raises():
    plugin = FilterPlugin(FilterPluginConfig(type="filter", block_tools=["delete_*"]))
    params = make_call_params("delete_file")
    with pytest.raises(McpError) as exc:
        await plugin.on_call_tool_request(params)
    assert "delete_file" in str(exc.value)


@pytest.mark.asyncio
async def test_filter_allow_tool_call_passes():
    plugin = FilterPlugin(FilterPluginConfig(type="filter", allow_tools=["read_*"]))
    params = make_call_params("read_file")
    result = await plugin.on_call_tool_request(params)
    assert result is params


@pytest.mark.asyncio
async def test_filter_allow_tool_call_blocks_unmatched():
    plugin = FilterPlugin(FilterPluginConfig(type="filter", allow_tools=["read_*"]))
    params = make_call_params("write_file")
    with pytest.raises(McpError):
        await plugin.on_call_tool_request(params)


@pytest.mark.asyncio
async def test_filter_no_policy_allows_all():
    plugin = FilterPlugin(FilterPluginConfig(type="filter"))
    params = make_call_params("anything")
    result = await plugin.on_call_tool_request(params)
    assert result is params


@pytest.mark.asyncio
async def test_filter_block_resources():
    plugin = FilterPlugin(FilterPluginConfig(type="filter", block_resources=["file:///secret/*"]))
    resources = [
        make_resource("file:///secret/key"),
        make_resource("file:///public/readme"),
    ]
    result = await plugin.on_list_resources(resources)
    assert len(result) == 1
    assert str(result[0].uri) == "file:///public/readme"


# ---------------------------------------------------------------------------
# FilterPlugin — hide_blocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_hide_blocked_true_hides_from_list():
    """Default: blocked tools are hidden from on_list_tools."""
    plugin = FilterPlugin(FilterPluginConfig(type="filter", block_tools=["write_*"]))
    assert plugin.hide_blocked is True
    tools = [make_tool("read_file"), make_tool("write_file")]
    result = await plugin.on_list_tools(tools)
    assert [t.name for t in result] == ["read_file"]


@pytest.mark.asyncio
async def test_filter_hide_blocked_false_shows_in_list():
    """hide_blocked=False: blocked tools appear in listings, calls still raise."""
    plugin = FilterPlugin(
        FilterPluginConfig(type="filter", block_tools=["write_*"], hide_blocked=False)
    )
    assert plugin.hide_blocked is False

    # on_list_tools should NOT filter
    tools = [make_tool("read_file"), make_tool("write_file")]
    result = await plugin.on_list_tools(tools)
    assert [t.name for t in result] == ["read_file", "write_file"]

    # on_call_tool_request should still block
    with pytest.raises(McpError):
        await plugin.on_call_tool_request(make_call_params("write_file"))


@pytest.mark.asyncio
async def test_filter_hide_blocked_false_resources():
    """hide_blocked=False: blocked resources appear in listings."""
    plugin = FilterPlugin(
        FilterPluginConfig(type="filter", block_resources=["file:///secret/*"], hide_blocked=False)
    )
    resources = [make_resource("file:///secret/key"), make_resource("file:///public/readme")]
    result = await plugin.on_list_resources(resources)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_filter_hide_blocked_false_prompts():
    """hide_blocked=False: blocked prompts appear in listings."""
    plugin = FilterPlugin(
        FilterPluginConfig(type="filter", block_prompts=["admin_*"], hide_blocked=False)
    )
    prompts = [make_prompt("admin_reset"), make_prompt("greeting")]
    result = await plugin.on_list_prompts(prompts)
    assert [p.name for p in result] == ["admin_reset", "greeting"]


def test_filter_is_tool_allowed_block():
    plugin = FilterPlugin(FilterPluginConfig(type="filter", block_tools=["write_*", "delete_*"]))
    assert plugin.is_tool_allowed("read_file") is True
    assert plugin.is_tool_allowed("write_file") is False
    assert plugin.is_tool_allowed("delete_dir") is False


def test_filter_is_tool_allowed_allow():
    plugin = FilterPlugin(FilterPluginConfig(type="filter", allow_tools=["read_*"]))
    assert plugin.is_tool_allowed("read_file") is True
    assert plugin.is_tool_allowed("write_file") is False


def test_filter_is_resource_allowed():
    plugin = FilterPlugin(FilterPluginConfig(type="filter", block_resources=["file:///secret/*"]))
    assert plugin.is_resource_allowed("file:///public/readme") is True
    assert plugin.is_resource_allowed("file:///secret/key") is False


def test_filter_is_prompt_allowed():
    plugin = FilterPlugin(FilterPluginConfig(type="filter", allow_prompts=["greet_*"]))
    assert plugin.is_prompt_allowed("greet_user") is True
    assert plugin.is_prompt_allowed("admin_reset") is False


# ---------------------------------------------------------------------------
# PluginBase — hide_blocked defaults
# ---------------------------------------------------------------------------


def test_plugin_base_hide_blocked_default():
    plugin = PluginBase()
    assert plugin.hide_blocked is True
    assert plugin.is_tool_allowed("anything") is True
    assert plugin.is_resource_allowed("file:///anything") is True
    assert plugin.is_prompt_allowed("anything") is True


# ---------------------------------------------------------------------------
# RewritePlugin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rewrite_renames_tool_in_list():
    plugin = RewritePlugin(
        RewritePluginConfig(type="rewrite", tool_renames={"read_file": "read_document"})
    )
    tools = [make_tool("read_file"), make_tool("write_file")]
    result = await plugin.on_list_tools(tools)
    assert [t.name for t in result] == ["read_document", "write_file"]


@pytest.mark.asyncio
async def test_rewrite_translates_call_to_upstream():
    plugin = RewritePlugin(
        RewritePluginConfig(type="rewrite", tool_renames={"read_file": "read_document"})
    )
    params = make_call_params("read_document", {"path": "/foo"})
    result = await plugin.on_call_tool_request(params)
    assert result.name == "read_file"
    assert result.arguments == {"path": "/foo"}


@pytest.mark.asyncio
async def test_rewrite_arg_overrides_injected():
    plugin = RewritePlugin(
        RewritePluginConfig(
            type="rewrite",
            argument_overrides={"read_file": {"encoding": "utf-8"}},
        )
    )
    params = make_call_params("read_file", {"path": "/foo"})
    result = await plugin.on_call_tool_request(params)
    assert result.arguments == {"path": "/foo", "encoding": "utf-8"}


@pytest.mark.asyncio
async def test_rewrite_arg_overrides_win():
    plugin = RewritePlugin(
        RewritePluginConfig(
            type="rewrite",
            argument_overrides={"tool": {"key": "forced"}},
        )
    )
    params = make_call_params("tool", {"key": "user_value"})
    result = await plugin.on_call_tool_request(params)
    assert result.arguments == {"key": "forced"}


@pytest.mark.asyncio
async def test_rewrite_response_prefix():
    plugin = RewritePlugin(RewritePluginConfig(type="rewrite", response_prefix="Info: "))
    params = make_call_params("tool")
    result = make_tool_result("hello world")
    modified = await plugin.on_call_tool_response(params, result)
    assert isinstance(modified.content[0], mt.TextContent)
    assert modified.content[0].text == "Info: hello world"


@pytest.mark.asyncio
async def test_rewrite_no_rename_passthrough():
    plugin = RewritePlugin(RewritePluginConfig(type="rewrite"))
    params = make_call_params("other_tool", {"x": 1})
    result = await plugin.on_call_tool_request(params)
    assert result is params


# ---------------------------------------------------------------------------
# JsonlLoggingPlugin
# ---------------------------------------------------------------------------


def make_image_result() -> ToolResult:
    return ToolResult(
        content=[mt.ImageContent(type="image", data="abc123==", mimeType="image/png")]
    )


def make_mixed_result(text: str) -> ToolResult:
    return ToolResult(
        content=[
            mt.TextContent(type="text", text=text),
            mt.ImageContent(type="image", data="abc123==", mimeType="image/png"),
        ]
    )


@pytest.mark.asyncio
async def test_logging_writes_paired_entry(tmp_path):
    log_file = tmp_path / "test.jsonl"
    plugin = JsonlLoggingPlugin(LoggingPluginConfig(type="logging", log_file=str(log_file)))

    params = make_call_params("my_tool", {"arg": "val"})
    params = await plugin.on_call_tool_request(params)

    # Request hook should not write anything
    assert not log_file.exists() or log_file.read_text().strip() == ""

    result = make_tool_result("done")
    await plugin.on_call_tool_response(params, result)

    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1

    entry = json.loads(lines[0])
    assert entry["schema_version"] == 2
    assert entry["method"] == "tools/call"
    assert entry["tool_name"] == "my_tool"
    assert entry["arguments"] == {"arg": "val"}
    assert entry["content_blocks"] == 1
    assert entry["duration_ms"] is not None


@pytest.mark.asyncio
async def test_logging_excludes_payloads(tmp_path):
    log_file = tmp_path / "test.jsonl"
    plugin = JsonlLoggingPlugin(
        LoggingPluginConfig(type="logging", log_file=str(log_file), include_payloads=False)
    )

    params = make_call_params("my_tool", {"secret": "value"})
    params = await plugin.on_call_tool_request(params)
    result = make_tool_result("done")
    await plugin.on_call_tool_response(params, result)

    lines = log_file.read_text().strip().splitlines()
    entry = json.loads(lines[0])
    assert entry["arguments"] is None


@pytest.mark.asyncio
async def test_logging_method_filter(tmp_path):
    log_file = tmp_path / "test.jsonl"
    plugin = JsonlLoggingPlugin(
        LoggingPluginConfig(
            type="logging",
            log_file=str(log_file),
            methods=["tools/list"],
        )
    )

    # tools/call should NOT be logged
    params = make_call_params("my_tool")
    params = await plugin.on_call_tool_request(params)
    result = make_tool_result("done")
    await plugin.on_call_tool_response(params, result)
    assert not log_file.exists() or log_file.read_text().strip() == ""

    # tools/list SHOULD be logged
    await plugin.on_list_tools([make_tool("foo")])
    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["method"] == "tools/list"


@pytest.mark.asyncio
async def test_logging_list_tools(tmp_path):
    log_file = tmp_path / "test.jsonl"
    plugin = JsonlLoggingPlugin(LoggingPluginConfig(type="logging", log_file=str(log_file)))

    tools = [make_tool("foo"), make_tool("bar")]
    result = await plugin.on_list_tools(tools)
    assert result == tools

    lines = log_file.read_text().strip().splitlines()
    entry = json.loads(lines[0])
    assert entry["item_count"] == 2
    assert entry["items"] == ["foo", "bar"]


@pytest.mark.asyncio
async def test_logging_rotation(tmp_path):
    log_file = tmp_path / "test.jsonl"
    plugin = JsonlLoggingPlugin(
        LoggingPluginConfig(
            type="logging",
            log_file=str(log_file),
            max_bytes=200,
            max_backups=3,
        )
    )

    # Write enough entries to trigger rotation
    for i in range(20):
        params = make_call_params(f"tool_{i}", {"i": i})
        params = await plugin.on_call_tool_request(params)
        result = make_tool_result(f"result_{i}")
        await plugin.on_call_tool_response(params, result)

    # Current file should exist and be under the limit
    assert log_file.exists()
    assert log_file.stat().st_size < 400  # should have rotated well before this

    # At least one backup should exist
    backup1 = log_file.with_suffix(".jsonl.1")
    assert backup1.exists()

    # Should not exceed max_backups
    excess = log_file.with_suffix(".jsonl.4")
    assert not excess.exists()

    # All backup files should contain valid JSONL
    for i in range(1, 4):
        backup = log_file.with_suffix(f".jsonl.{i}")
        if backup.exists():
            for line in backup.read_text().strip().splitlines():
                entry = json.loads(line)
                assert entry["schema_version"] == 2


@pytest.mark.asyncio
async def test_logging_no_rotation_by_default(tmp_path):
    log_file = tmp_path / "test.jsonl"
    plugin = JsonlLoggingPlugin(LoggingPluginConfig(type="logging", log_file=str(log_file)))

    for i in range(10):
        params = make_call_params(f"tool_{i}")
        params = await plugin.on_call_tool_request(params)
        result = make_tool_result(f"result_{i}")
        await plugin.on_call_tool_response(params, result)

    # All entries in one file, no backups
    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 10
    assert not log_file.with_suffix(".jsonl.1").exists()


# ---------------------------------------------------------------------------
# InventoryPlugin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inventory_writes_tools_snapshot(tmp_path):
    inv_file = tmp_path / "inventory.json"
    plugin = InventoryPlugin(InventoryPluginConfig(type="inventory", inventory_file=str(inv_file)))

    tools = [make_tool("foo"), make_tool("bar")]
    result = await plugin.on_list_tools(tools)
    assert result is tools

    snapshot = json.loads(inv_file.read_text())
    assert "ts" in snapshot
    assert len(snapshot["tools"]) == 2
    assert snapshot["tools"][0]["name"] == "foo"
    assert snapshot["tools"][0]["description"] == "test"
    assert snapshot["tools"][0]["parameters"] == {"type": "object", "properties": {}}
    assert "resources" not in snapshot
    assert "prompts" not in snapshot


@pytest.mark.asyncio
async def test_inventory_writes_resources_snapshot(tmp_path):
    inv_file = tmp_path / "inventory.json"
    plugin = InventoryPlugin(InventoryPluginConfig(type="inventory", inventory_file=str(inv_file)))

    resources = [make_resource("file:///a"), make_resource("file:///b")]
    result = await plugin.on_list_resources(resources)
    assert result is resources

    snapshot = json.loads(inv_file.read_text())
    assert len(snapshot["resources"]) == 2
    assert snapshot["resources"][0]["uri"] == "file:///a"


@pytest.mark.asyncio
async def test_inventory_writes_prompts_snapshot(tmp_path):
    inv_file = tmp_path / "inventory.json"
    plugin = InventoryPlugin(InventoryPluginConfig(type="inventory", inventory_file=str(inv_file)))

    prompts = [make_prompt("greeting"), make_prompt("farewell")]
    result = await plugin.on_list_prompts(prompts)
    assert result is prompts

    snapshot = json.loads(inv_file.read_text())
    assert len(snapshot["prompts"]) == 2
    assert snapshot["prompts"][0]["name"] == "greeting"


@pytest.mark.asyncio
async def test_inventory_accumulates_across_hooks(tmp_path):
    inv_file = tmp_path / "inventory.json"
    plugin = InventoryPlugin(InventoryPluginConfig(type="inventory", inventory_file=str(inv_file)))

    await plugin.on_list_tools([make_tool("t1")])
    snapshot = json.loads(inv_file.read_text())
    assert "tools" in snapshot
    assert "resources" not in snapshot

    await plugin.on_list_resources([make_resource("file:///r1")])
    snapshot = json.loads(inv_file.read_text())
    assert "tools" in snapshot
    assert "resources" in snapshot

    await plugin.on_list_prompts([make_prompt("p1")])
    snapshot = json.loads(inv_file.read_text())
    assert len(snapshot["tools"]) == 1
    assert len(snapshot["resources"]) == 1
    assert len(snapshot["prompts"]) == 1


# ---------------------------------------------------------------------------
# JsonlLoggingPlugin — payload offload and binary handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logging_offloads_large_payload(tmp_path):
    log_file = tmp_path / "test.jsonl"
    plugin = JsonlLoggingPlugin(
        LoggingPluginConfig(type="logging", log_file=str(log_file), payload_offload_chars=10)
    )
    params = make_call_params("my_tool")
    params = await plugin.on_call_tool_request(params)
    result = make_tool_result("this is a long response that exceeds ten chars")
    await plugin.on_call_tool_response(params, result)

    entry = json.loads(log_file.read_text().strip())
    assert "response_payload" not in entry
    assert "payload_file" in entry

    sidecar = tmp_path / entry["payload_file"]
    assert sidecar.exists()
    assert "long response" in sidecar.read_text()


@pytest.mark.asyncio
async def test_logging_inline_payload_below_threshold(tmp_path):
    log_file = tmp_path / "test.jsonl"
    plugin = JsonlLoggingPlugin(
        LoggingPluginConfig(type="logging", log_file=str(log_file), payload_offload_chars=1000)
    )
    params = make_call_params("my_tool")
    params = await plugin.on_call_tool_request(params)
    result = make_tool_result("short")
    await plugin.on_call_tool_response(params, result)

    entry = json.loads(log_file.read_text().strip())
    assert entry["response_payload"] == "short"
    assert "payload_file" not in entry


@pytest.mark.asyncio
async def test_logging_json_payload_unpacked(tmp_path):
    log_file = tmp_path / "test.jsonl"
    plugin = JsonlLoggingPlugin(LoggingPluginConfig(type="logging", log_file=str(log_file)))
    params = make_call_params("my_tool")
    params = await plugin.on_call_tool_request(params)
    result = make_tool_result('{"key": "value", "count": 42}')
    await plugin.on_call_tool_response(params, result)

    entry = json.loads(log_file.read_text().strip())
    assert entry["response_payload"] == {"key": "value", "count": 42}


@pytest.mark.asyncio
async def test_logging_non_json_payload_kept_as_string(tmp_path):
    log_file = tmp_path / "test.jsonl"
    plugin = JsonlLoggingPlugin(LoggingPluginConfig(type="logging", log_file=str(log_file)))
    params = make_call_params("my_tool")
    params = await plugin.on_call_tool_request(params)
    result = make_tool_result("plain text response")
    await plugin.on_call_tool_response(params, result)

    entry = json.loads(log_file.read_text().strip())
    assert entry["response_payload"] == "plain text response"


@pytest.mark.asyncio
async def test_logging_binary_omitted_by_default(tmp_path):
    log_file = tmp_path / "test.jsonl"
    plugin = JsonlLoggingPlugin(LoggingPluginConfig(type="logging", log_file=str(log_file)))
    params = make_call_params("my_tool")
    params = await plugin.on_call_tool_request(params)
    await plugin.on_call_tool_response(params, make_image_result())

    entry = json.loads(log_file.read_text().strip())
    assert entry["binary_content"] is True
    assert "response_payload" not in entry
    assert "binary_payload_file" not in entry
    payloads_dir = tmp_path / "test_payloads"
    assert not payloads_dir.exists()


@pytest.mark.asyncio
async def test_logging_binary_saved_when_enabled(tmp_path):
    log_file = tmp_path / "test.jsonl"
    plugin = JsonlLoggingPlugin(
        LoggingPluginConfig(type="logging", log_file=str(log_file), include_binary_payloads=True)
    )
    params = make_call_params("my_tool")
    params = await plugin.on_call_tool_request(params)
    await plugin.on_call_tool_response(params, make_image_result())

    entry = json.loads(log_file.read_text().strip())
    assert entry["binary_content"] is True
    assert "binary_payload_file" in entry

    sidecar = tmp_path / entry["binary_payload_file"]
    assert sidecar.exists()
    data = json.loads(sidecar.read_text())
    assert data[0]["type"] == "image"
    assert data[0]["mimeType"] == "image/png"


@pytest.mark.asyncio
async def test_logging_mixed_response(tmp_path):
    log_file = tmp_path / "test.jsonl"
    plugin = JsonlLoggingPlugin(
        LoggingPluginConfig(
            type="logging",
            log_file=str(log_file),
            payload_offload_chars=5,
            include_binary_payloads=True,
        )
    )
    params = make_call_params("my_tool")
    params = await plugin.on_call_tool_request(params)
    await plugin.on_call_tool_response(params, make_mixed_result("hello world"))

    entry = json.loads(log_file.read_text().strip())
    assert entry["binary_content"] is True
    assert "payload_file" in entry  # text offloaded (>5 chars)
    assert "binary_payload_file" in entry  # binary saved


@pytest.mark.asyncio
async def test_logging_rotation_deletes_payloads_dir(tmp_path):
    log_file = tmp_path / "test.jsonl"
    plugin = JsonlLoggingPlugin(
        LoggingPluginConfig(
            type="logging",
            log_file=str(log_file),
            max_bytes=50,
            max_backups=1,
            payload_offload_chars=1,
        )
    )
    # First entry: causes a payload sidecar to be written, then rotation
    params = make_call_params("my_tool")
    params = await plugin.on_call_tool_request(params)
    await plugin.on_call_tool_response(params, make_tool_result("hello"))
    # Force a second rotation to push the first backup (and its payloads) out
    params2 = make_call_params("my_tool")
    params2 = await plugin.on_call_tool_request(params2)
    await plugin.on_call_tool_response(params2, make_tool_result("world"))

    # After two rotations with max_backups=1, the first payloads dir must be gone
    assert not (tmp_path / "test_payloads.2").exists()


@pytest.mark.asyncio
async def test_logging_rotation_renames_payloads_dir(tmp_path):
    log_file = tmp_path / "test.jsonl"
    plugin = JsonlLoggingPlugin(
        LoggingPluginConfig(
            type="logging",
            log_file=str(log_file),
            max_bytes=50,
            max_backups=2,
            payload_offload_chars=1,
        )
    )
    params = make_call_params("my_tool")
    params = await plugin.on_call_tool_request(params)
    await plugin.on_call_tool_response(params, make_tool_result("hello"))

    payloads_dir = tmp_path / "test_payloads"
    backup1 = tmp_path / "test_payloads.1"

    # After rotation, current payloads dir should be renamed to .1
    assert not payloads_dir.exists()
    assert backup1.exists()


# ---------------------------------------------------------------------------
# NotionAccessPlugin — notion-upload-image synthetic tool
# ---------------------------------------------------------------------------


def make_notion_access_plugin(notion_token: str | None = None):
    from mcp_proxy.config.schema import NotionAccessPluginConfig
    from mcp_proxy.plugins.notion_access_plugin import NotionAccessPlugin

    return NotionAccessPlugin(
        NotionAccessPluginConfig(
            type="notion_access",
            bot_name="TestBot",
            notion_token=notion_token,
        )
    )


@pytest.mark.asyncio
async def test_upload_image_no_token_emits_warning(caplog):
    """register_tools logs a warning and does not register the tool when token is absent."""
    import logging

    from fastmcp import Client, FastMCP

    plugin = make_notion_access_plugin(notion_token=None)
    server = FastMCP("test")

    with caplog.at_level(logging.WARNING):
        plugin.register_tools(server)

    assert any("notion_token not configured" in r.message for r in caplog.records)
    # Tool should not be registered
    async with Client(server) as client:
        tools = await client.list_tools()
    tool_names = {t.name for t in tools}
    assert "notion-upload-image" not in tool_names
    assert "notion-delete-image" not in tool_names


@pytest.mark.asyncio
async def test_image_tools_registered_with_token():
    from fastmcp import Client, FastMCP

    plugin = make_notion_access_plugin(notion_token="secret-token")
    server = FastMCP("test")
    plugin.register_tools(server)

    async with Client(server) as client:
        tools = await client.list_tools()

    tool_names = {t.name for t in tools}
    assert "notion-upload-image" in tool_names
    assert "notion-delete-image" in tool_names


@pytest.mark.asyncio
async def test_upload_image_requires_write_permission(tmp_path):
    """notion-upload-image raises ToolError if page has no cached WRITE permission."""
    from fastmcp import Client, FastMCP
    from fastmcp.exceptions import ToolError

    plugin = make_notion_access_plugin(notion_token="secret-token")
    server = FastMCP("test")
    plugin.register_tools(server)

    # Create a dummy image file
    img = tmp_path / "photo.png"
    img.write_bytes(b"\x89PNG\r\n")

    # Call the tool without fetching the page first (no cached permission)
    async with Client(server) as client:
        with pytest.raises(ToolError, match="not cached"):
            await client.call_tool(
                "notion-upload-image",
                {"page_id": "page-abc", "file_path": str(img)},
            )


@pytest.mark.asyncio
async def test_upload_image_success(tmp_path):
    """notion-upload-image uploads file and inserts image block, replacing the placeholder."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from fastmcp import Client, FastMCP

    from mcp_proxy.plugins.notion_access_plugin import AccessLevel, CachedPermission

    page_id = "page-abc"
    file_path = tmp_path / "photo.png"
    file_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    plugin = make_notion_access_plugin(notion_token="tok-123")
    # Seed the cache with WRITE permission
    plugin._cache[page_id] = CachedPermission(
        level=AccessLevel.WRITE,
        first_line="TestBot 🖊",
        expires_at=float("inf"),
    )

    server = FastMCP("test")
    plugin.register_tools(server)

    placeholder = f"[IMAGE_UPLOAD: {file_path}]"
    block_id = "blk-xyz"

    # Build mock HTTP responses in order:
    # GET  /blocks/{page_id}/children → page with placeholder paragraph
    # POST /file_uploads              → {id, upload_url}
    # PUT  {upload_url}               → 200
    # DELETE /blocks/{block_id}       → 200
    # POST /blocks/{page_id}/children → 200
    def make_response(json_data=None):
        r = MagicMock()
        r.json = MagicMock(return_value=json_data or {})
        r.raise_for_status = MagicMock()
        return r

    responses = [
        make_response(
            {
                "results": [
                    {
                        "id": block_id,
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"plain_text": placeholder}]},
                    }
                ]
            }
        ),
        make_response({"id": "file-id-1", "upload_url": "https://s3.example.com/upload"}),
        make_response(),
        make_response(),
        make_response(),
    ]

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=responses[0])
    mock_client.post = AsyncMock(side_effect=[responses[1], responses[4]])
    mock_client.put = AsyncMock(return_value=responses[2])
    mock_client.delete = AsyncMock(return_value=responses[3])

    with patch(
        "mcp_proxy.plugins.notion_access_plugin.image_tools.httpx.AsyncClient",
        return_value=mock_client,
    ):
        async with Client(server) as client:
            result = await client.call_tool(
                "notion-upload-image",
                {"page_id": page_id, "file_path": str(file_path), "caption": "A test image"},
            )

    assert isinstance(result.content[0], mt.TextContent)
    text = result.content[0].text
    assert "photo.png" in text
    assert page_id in text

    # Verify delete was called on the placeholder block
    mock_client.delete.assert_called_once()
    delete_url = mock_client.delete.call_args[0][0]
    assert block_id in delete_url
