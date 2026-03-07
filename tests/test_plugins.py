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
