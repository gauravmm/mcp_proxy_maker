"""Hive workspace access control plugin.

Enforces project-level scoping on a Hive MCP upstream:

- Injects ``workspaceId`` into all tools that accept it.
- Restricts ``getActions.projectIds`` to a configured allowlist; allows the
  agent to narrow within the allowlist but never to widen beyond it.
- Enforces per-action ``projectId`` on ``insertActions``.
- Verifies that ``actionIds`` on write tools belong to allowed projects, using
  a session-lifetime action→project cache populated from ``getActions`` responses.
- Blocks ``getProjects.includePrivate``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import mcp.types as mt
from fastmcp.tools.tool import ToolResult
from mcp import McpError
from mcp.types import ErrorData

from ..config.schema import HiveAccessPluginConfig
from .base import PluginBase

if TYPE_CHECKING:
    from fastmcp import Client

_ERR_DENIED = -32601
_log = logging.getLogger(__name__)

# Tools that modify actions by actionIds (no projectId in params).
_WRITE_BY_ACTION_IDS = {
    "updateActionsStatus",
    "updateActionsTitles",
    "updateActionsDescription",
    "updateActionsAssignees",
    "updateActionsLabels",
    "updateActionsMilestone",
    "updateActionsPriorityLevelId",
}

_WORKSPACE_SCOPED_TOOLS = {
    "getWorkspace",
    "getProjects",
    "getActions",
    "getNotebooks",
    "insertActions",
    *_WRITE_BY_ACTION_IDS,
}


def _extract_text(result: ToolResult) -> str:
    parts = []
    for block in result.content or []:
        if isinstance(block, mt.TextContent):
            parts.append(block.text)
    return "\n".join(parts)


def _parse_actions_json(text: str) -> list[dict[str, Any]]:
    """Try to extract a list of action objects from a getActions response.

    Hive may return actions in several formats:
    - ``{"actions": [...]}`` or ``{"data": [...]}`` or ``{"results": [...]}``
    - A bare JSON list ``[...]``
    - GraphQL-style ``{"edges": [{"node": {...}}, ...]}``
    """
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # GraphQL edges/node format.
        edges = data.get("edges")
        if isinstance(edges, list):
            return [
                e["node"] for e in edges if isinstance(e, dict) and isinstance(e.get("node"), dict)
            ]
        for key in ("actions", "data", "results"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


class HiveAccessPlugin(PluginBase):
    """Enforces workspace + project scope on a Hive MCP upstream."""

    def __init__(self, config: HiveAccessPluginConfig) -> None:
        self._workspace_id = config.workspace_id
        self._allowed_project_ids: frozenset[str] = frozenset(config.allowed_project_ids)
        self.hide_blocked = config.hide_blocked
        # actionId -> projectId; valid for session lifetime (actions are immutable re: project).
        self._action_project_cache: dict[str, str] = {}
        self._client: Client | None = None

    # ------------------------------------------------------------------
    # Upstream client (optional — enables write-tool verification)
    # ------------------------------------------------------------------

    def set_upstream_client(self, client: object) -> None:
        from fastmcp import Client

        if isinstance(client, Client):
            self._client = client

    # ------------------------------------------------------------------
    # on_call_tool_request
    # ------------------------------------------------------------------

    async def on_call_tool_request(
        self, params: mt.CallToolRequestParams
    ) -> mt.CallToolRequestParams:
        name = params.name
        args = dict(params.arguments or {})

        if name in _WORKSPACE_SCOPED_TOOLS:
            args["workspaceId"] = self._workspace_id

        if name == "getWorkspace":
            pass

        elif name == "getProjects":
            if args.get("includePrivate"):
                raise McpError(
                    ErrorData(code=_ERR_DENIED, message="includePrivate is not permitted.")
                )
            # Inject the full allowlist; allow the agent to narrow but not widen.
            requested = args.get("specificIds")
            if requested is None:
                args["specificIds"] = list(self._allowed_project_ids)
            else:
                clamped = [p for p in requested if p in self._allowed_project_ids]
                if not clamped:
                    raise McpError(
                        ErrorData(
                            code=_ERR_DENIED,
                            message="None of the requested project IDs are in the allowed set.",
                        )
                    )
                args["specificIds"] = clamped

        elif name == "getActions":
            args = self._enforce_project_ids(args)

        elif name == "getNotebooks":
            pass

        elif name == "insertActions":
            args = self._enforce_insert_actions(args)

        elif name in _WRITE_BY_ACTION_IDS:
            if name == "updateActionsTitles":
                updates = args.get("actionTitleUpdates") or []
                action_ids = [
                    u["actionId"] for u in updates if isinstance(u, dict) and u.get("actionId")
                ]
            else:
                action_ids = args.get("actionIds") or []
            if not action_ids:
                raise McpError(ErrorData(code=_ERR_DENIED, message="actionIds must be non-empty."))
            await self._verify_action_ids(action_ids)

        return mt.CallToolRequestParams(name=name, arguments=args)

    # ------------------------------------------------------------------
    # on_call_tool_response: populate action→project cache from getActions
    # ------------------------------------------------------------------

    async def on_call_tool_response(
        self,
        params: mt.CallToolRequestParams,
        result: ToolResult,
    ) -> ToolResult:
        if params.name != "getActions":
            return result
        text = _extract_text(result)
        if text:
            for action in _parse_actions_json(text):
                action_id = action.get("id") or action.get("_id")
                project_id = action.get("projectId")
                if action_id and project_id:
                    self._action_project_cache[str(action_id)] = str(project_id)
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _enforce_project_ids(self, args: dict[str, Any]) -> dict[str, Any]:
        """Clamp getActions.projectIds to the allowlist."""
        if "projectIds" not in args:
            # No filter specified — inject the full allowlist.
            args["projectIds"] = list(self._allowed_project_ids)
            return args

        requested = args["projectIds"]

        # Block explicit null — would return project-less / all-workspace actions.
        if not isinstance(requested, list):
            raise McpError(
                ErrorData(
                    code=_ERR_DENIED,
                    message="projectIds must be a list of project IDs, not null.",
                )
            )

        clamped = [p for p in requested if p in self._allowed_project_ids]
        if not clamped:
            raise McpError(
                ErrorData(
                    code=_ERR_DENIED,
                    message=(
                        "None of the requested projectIds are in the allowed set. "
                        f"Allowed: {sorted(self._allowed_project_ids)}"
                    ),
                )
            )
        args["projectIds"] = clamped
        return args

    def _enforce_insert_actions(self, args: dict[str, Any]) -> dict[str, Any]:
        """Require projectId on every action in insertActions to be in the allowlist."""
        actions = args.get("actions") or []
        for i, action in enumerate(actions):
            if not isinstance(action, dict):
                continue
            pid = action.get("projectId")
            if not pid:
                raise McpError(
                    ErrorData(
                        code=_ERR_DENIED,
                        message=f"Action at index {i} is missing projectId.",
                    )
                )
            if pid not in self._allowed_project_ids:
                raise McpError(
                    ErrorData(
                        code=_ERR_DENIED,
                        message=f"projectId '{pid}' is not in the allowed set.",
                    )
                )
        return args

    async def _verify_action_ids(self, action_ids: list[str]) -> None:
        """Verify all actionIds belong to allowed projects.

        Uses the session-lifetime cache; fetches missing IDs from upstream when
        an upstream client is available.
        """
        missing = [aid for aid in action_ids if aid not in self._action_project_cache]

        if missing:
            if self._client is None:
                _log.warning(
                    "hive_access: cannot verify actionIds %s — no upstream client. Allowing.",
                    missing,
                )
            else:
                try:
                    result = await self._client.call_tool(
                        "getActions",
                        {"specificIds": missing, "workspaceId": self._workspace_id},
                    )
                    parts = [
                        block.text
                        for block in (result.content or [])
                        if isinstance(block, mt.TextContent)
                    ]
                    text = "\n".join(parts)
                    for action in _parse_actions_json(text):
                        action_id = action.get("id") or action.get("_id")
                        project_id = action.get("projectId")
                        if action_id and project_id:
                            self._action_project_cache[str(action_id)] = str(project_id)
                except Exception as exc:
                    _log.warning("hive_access: getActions verification call failed: %s", exc)

        # Now check all IDs we can resolve.
        for aid in action_ids:
            pid = self._action_project_cache.get(aid)
            if pid is None:
                # Still missing after fetch attempt — conservative: block.
                raise McpError(
                    ErrorData(
                        code=_ERR_DENIED,
                        message=(
                            f"Action '{aid}' could not be verified as belonging to an allowed "
                            "project. Fetch the action via getActions first."
                        ),
                    )
                )
            if pid not in self._allowed_project_ids:
                raise McpError(
                    ErrorData(
                        code=_ERR_DENIED,
                        message=(
                            f"Action '{aid}' belongs to project '{pid}' which is not in the "
                            "allowed set."
                        ),
                    )
                )
