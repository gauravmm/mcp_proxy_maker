"""Tests for the Hive workspace access control plugin."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import mcp.types as mt
import pytest
from fastmcp.tools.tool import ToolResult
from mcp import McpError

from mcp_proxy.config.schema import HiveAccessPluginConfig
from mcp_proxy.plugins.hive_access_plugin import HiveAccessPlugin

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WS_ID = "workspace123"
PROJECT_A = "projectA"
PROJECT_B = "projectB"
PROJECT_X = "projectX"  # NOT in allowlist


def _cfg(**overrides) -> HiveAccessPluginConfig:
    defaults = dict(
        type="hive_access",
        workspace_id=WS_ID,
        allowed_project_ids=[PROJECT_A, PROJECT_B],
    )
    defaults.update(overrides)
    return HiveAccessPluginConfig(**defaults)


def _plugin(**overrides) -> HiveAccessPlugin:
    return HiveAccessPlugin(_cfg(**overrides))


def _call(name: str, arguments: dict[str, Any] | None = None) -> mt.CallToolRequestParams:
    return mt.CallToolRequestParams(name=name, arguments=arguments)


def _result(text: str) -> ToolResult:
    return ToolResult(content=[mt.TextContent(type="text", text=text)])


def _actions_json(*actions: dict[str, Any]) -> str:
    return json.dumps({"actions": list(actions)})


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_config_basic():
    cfg = _cfg()
    assert cfg.workspace_id == WS_ID
    assert set(cfg.allowed_project_ids) == {PROJECT_A, PROJECT_B}
    assert cfg.hide_blocked is True


# ---------------------------------------------------------------------------
# getWorkspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_workspace_injects_workspace_id():
    p = _plugin()
    result = await p.on_call_tool_request(_call("getWorkspace"))
    assert result.arguments["workspaceId"] == WS_ID


@pytest.mark.asyncio
async def test_get_workspace_overrides_existing():
    p = _plugin()
    result = await p.on_call_tool_request(_call("getWorkspace", {"workspaceId": "other"}))
    assert result.arguments["workspaceId"] == WS_ID


# ---------------------------------------------------------------------------
# getProjects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_projects_injects_full_allowlist():
    p = _plugin()
    result = await p.on_call_tool_request(_call("getProjects"))
    assert result.arguments["workspaceId"] == WS_ID
    assert set(result.arguments["specificIds"]) == {PROJECT_A, PROJECT_B}


@pytest.mark.asyncio
async def test_get_projects_overrides_existing_workspace_id():
    p = _plugin()
    result = await p.on_call_tool_request(_call("getProjects", {"workspaceId": "other"}))
    assert result.arguments["workspaceId"] == WS_ID


@pytest.mark.asyncio
async def test_get_projects_narrows_to_subset():
    p = _plugin()
    result = await p.on_call_tool_request(_call("getProjects", {"specificIds": [PROJECT_A]}))
    assert result.arguments["specificIds"] == [PROJECT_A]


@pytest.mark.asyncio
async def test_get_projects_clamps_superset():
    p = _plugin()
    result = await p.on_call_tool_request(
        _call("getProjects", {"specificIds": [PROJECT_A, PROJECT_X]})
    )
    assert result.arguments["specificIds"] == [PROJECT_A]


@pytest.mark.asyncio
async def test_get_projects_blocks_fully_disallowed():
    p = _plugin()
    with pytest.raises(McpError, match="allowed set"):
        await p.on_call_tool_request(_call("getProjects", {"specificIds": [PROJECT_X]}))


@pytest.mark.asyncio
async def test_get_projects_blocks_include_private():
    p = _plugin()
    with pytest.raises(McpError, match="includePrivate"):
        await p.on_call_tool_request(_call("getProjects", {"includePrivate": True}))


# ---------------------------------------------------------------------------
# getActions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_actions_no_project_ids_injects_allowlist():
    p = _plugin()
    result = await p.on_call_tool_request(_call("getActions"))
    assert result.arguments["workspaceId"] == WS_ID
    assert set(result.arguments["projectIds"]) == {PROJECT_A, PROJECT_B}


@pytest.mark.asyncio
async def test_get_actions_overrides_existing_workspace_id():
    p = _plugin()
    result = await p.on_call_tool_request(_call("getActions", {"workspaceId": "other"}))
    assert result.arguments["workspaceId"] == WS_ID


@pytest.mark.asyncio
async def test_get_actions_subset_allowed():
    p = _plugin()
    result = await p.on_call_tool_request(_call("getActions", {"projectIds": [PROJECT_A]}))
    assert result.arguments["projectIds"] == [PROJECT_A]


@pytest.mark.asyncio
async def test_get_actions_superset_clamped():
    p = _plugin()
    result = await p.on_call_tool_request(
        _call("getActions", {"projectIds": [PROJECT_A, PROJECT_X]})
    )
    assert result.arguments["projectIds"] == [PROJECT_A]


@pytest.mark.asyncio
async def test_get_actions_fully_disallowed_raises():
    p = _plugin()
    with pytest.raises(McpError, match="allowed set"):
        await p.on_call_tool_request(_call("getActions", {"projectIds": [PROJECT_X]}))


@pytest.mark.asyncio
async def test_get_actions_null_project_ids_raises():
    p = _plugin()
    # Passing a non-list (e.g. None as explicit value) is blocked
    with pytest.raises(McpError, match="list"):
        await p.on_call_tool_request(_call("getActions", {"projectIds": None}))


# ---------------------------------------------------------------------------
# getActions response → cache population
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_actions_response_populates_cache():
    p = _plugin()
    params = _call("getActions", {"projectIds": [PROJECT_A]})
    payload = _actions_json(
        {"id": "action1", "projectId": PROJECT_A},
        {"id": "action2", "projectId": PROJECT_B},
    )
    await p.on_call_tool_response(params, _result(payload))
    assert p._action_project_cache["action1"] == PROJECT_A
    assert p._action_project_cache["action2"] == PROJECT_B


@pytest.mark.asyncio
async def test_get_actions_response_edges_format_populates_cache():
    """Hive may return actions in GraphQL edges/node format."""
    p = _plugin()
    params = _call("getActions", {"projectIds": [PROJECT_A]})
    payload = json.dumps(
        {
            "edges": [
                {"node": {"_id": "action1", "projectId": PROJECT_A}},
                {"node": {"_id": "action2", "projectId": PROJECT_B}},
            ],
            "pageInfo": {"hasNextPage": False},
        }
    )
    await p.on_call_tool_response(params, _result(payload))
    assert p._action_project_cache["action1"] == PROJECT_A
    assert p._action_project_cache["action2"] == PROJECT_B


@pytest.mark.asyncio
async def test_non_get_actions_response_not_cached():
    p = _plugin()
    params = _call("updateActionsStatus", {"actionIds": ["action1"]})
    await p.on_call_tool_response(params, _result("ok"))
    assert "action1" not in p._action_project_cache


# ---------------------------------------------------------------------------
# insertActions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_notebooks_overrides_existing_workspace_id():
    p = _plugin()
    result = await p.on_call_tool_request(_call("getNotebooks", {"workspaceId": "other"}))
    assert result.arguments["workspaceId"] == WS_ID


@pytest.mark.asyncio
async def test_insert_actions_allowed_project():
    p = _plugin()
    result = await p.on_call_tool_request(
        _call(
            "insertActions",
            {"workspaceId": "other", "actions": [{"projectId": PROJECT_A, "name": "Task"}]},
        )
    )
    assert result.arguments["workspaceId"] == WS_ID
    assert result.arguments["actions"][0]["projectId"] == PROJECT_A


@pytest.mark.asyncio
async def test_insert_actions_disallowed_project_raises():
    p = _plugin()
    with pytest.raises(McpError, match="not in the allowed set"):
        await p.on_call_tool_request(
            _call("insertActions", {"actions": [{"projectId": PROJECT_X, "name": "Task"}]})
        )


@pytest.mark.asyncio
async def test_insert_actions_missing_project_id_raises():
    p = _plugin()
    with pytest.raises(McpError, match="missing projectId"):
        await p.on_call_tool_request(_call("insertActions", {"actions": [{"name": "Task"}]}))


# ---------------------------------------------------------------------------
# Write tools (updateActions*) — cache-based verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_tool_cached_allowed_passes():
    p = _plugin()
    p._action_project_cache["action1"] = PROJECT_A
    result = await p.on_call_tool_request(
        _call("updateActionsStatus", {"workspaceId": "other", "actionIds": ["action1"]})
    )
    assert result.arguments["workspaceId"] == WS_ID


@pytest.mark.asyncio
async def test_write_tool_cached_disallowed_raises():
    p = _plugin()
    p._action_project_cache["action1"] = PROJECT_X
    with pytest.raises(McpError, match="not in the allowed"):
        await p.on_call_tool_request(_call("updateActionsStatus", {"actionIds": ["action1"]}))


@pytest.mark.asyncio
async def test_write_tool_uncached_no_client_allows_with_warning(caplog):
    p = _plugin()
    # No client, action not in cache — should warn and allow through.
    import logging

    with caplog.at_level(logging.WARNING, logger="mcp_proxy.plugins.hive_access_plugin"):
        with pytest.raises(McpError):
            # Still raises because after allowing fetch-failure, the ID remains missing.
            await p.on_call_tool_request(
                _call("updateActionsStatus", {"actionIds": ["unknown_action"]})
            )


@pytest.mark.asyncio
async def test_write_tool_uncached_with_client_fetches_and_verifies():
    p = _plugin()

    # Mock client that returns the action in an allowed project.
    mock_client = MagicMock()
    mock_client.call_tool = AsyncMock(
        return_value=MagicMock(
            content=[
                mt.TextContent(
                    type="text",
                    text=_actions_json({"id": "action99", "projectId": PROJECT_A}),
                )
            ]
        )
    )
    p._client = mock_client

    await p.on_call_tool_request(_call("updateActionsStatus", {"actionIds": ["action99"]}))
    assert p._action_project_cache["action99"] == PROJECT_A


@pytest.mark.asyncio
async def test_write_tool_uncached_with_client_disallowed_raises():
    p = _plugin()

    mock_client = MagicMock()
    mock_client.call_tool = AsyncMock(
        return_value=MagicMock(
            content=[
                mt.TextContent(
                    type="text",
                    text=_actions_json({"id": "actionX", "projectId": PROJECT_X}),
                )
            ]
        )
    )
    p._client = mock_client

    with pytest.raises(McpError, match="not in the allowed"):
        await p.on_call_tool_request(_call("updateActionsStatus", {"actionIds": ["actionX"]}))


@pytest.mark.asyncio
async def test_write_tool_empty_action_ids_raises():
    p = _plugin()
    with pytest.raises(McpError, match="non-empty"):
        await p.on_call_tool_request(_call("updateActionsStatus", {"actionIds": []}))


# ---------------------------------------------------------------------------
# Passthrough tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_passthrough_tool_unchanged():
    p = _plugin()
    params = _call("actionComments", {"actionId": "action1"})
    result = await p.on_call_tool_request(params)
    assert result.name == "actionComments"
    assert result.arguments == {"actionId": "action1"}
