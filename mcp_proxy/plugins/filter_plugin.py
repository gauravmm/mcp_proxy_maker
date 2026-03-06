"""Filter plugin: allow or block tools/resources/prompts by glob pattern."""

from __future__ import annotations

import fnmatch

import mcp.types as mt
from mcp import McpError
from mcp.types import ErrorData

from fastmcp.prompts.prompt import Prompt, PromptResult
from fastmcp.resources.resource import Resource, ResourceResult
from fastmcp.tools.tool import Tool, ToolResult

from ..config.schema import FilterPluginConfig
from .base import PluginBase

# MCP error code for "method/tool not found"
_ERR_NOT_FOUND = -32601


def _is_allowed(name: str, allow: list[str] | None, block: list[str] | None) -> bool:
    """Return True if `name` passes the allow/block policy.

    - If `allow` is set: the name must match at least one pattern.
    - If `block` is set: the name must NOT match any pattern.
    - If neither is set: all names are allowed.
    """
    if allow is not None:
        return any(fnmatch.fnmatch(name, p) for p in allow)
    if block is not None:
        return not any(fnmatch.fnmatch(name, p) for p in block)
    return True


class FilterPlugin(PluginBase):
    """Block or allow tools, resources, and prompts by name or glob pattern.

    Two modes per category (mutually exclusive, validated in config):

    - **Allow-list** (``allow_tools``): only the listed patterns are exposed;
      everything else is hidden and blocked.
    - **Deny-list** (``block_tools``): the listed patterns are hidden and
      blocked; everything else passes through.

    Policy is enforced in two places:
    1. ``on_list_*`` — hides disallowed items from the listing.
    2. ``on_*_request`` — blocks disallowed calls with ``McpError``.
    """

    def __init__(self, config: FilterPluginConfig) -> None:
        self._tool_allow = config.allow_tools
        self._tool_block = config.block_tools
        self._resource_allow = config.allow_resources
        self._resource_block = config.block_resources
        self._prompt_allow = config.allow_prompts
        self._prompt_block = config.block_prompts

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    async def on_call_tool_request(
        self, params: mt.CallToolRequestParams
    ) -> mt.CallToolRequestParams:
        if not _is_allowed(params.name, self._tool_allow, self._tool_block):
            raise McpError(
                ErrorData(
                    code=_ERR_NOT_FOUND,
                    message=f"Tool blocked by policy: {params.name}",
                )
            )
        return params

    async def on_list_tools(self, tools: list[Tool]) -> list[Tool]:
        return [
            t for t in tools
            if _is_allowed(t.name, self._tool_allow, self._tool_block)
        ]

    # ------------------------------------------------------------------
    # Resources
    # ------------------------------------------------------------------

    async def on_read_resource_request(
        self, params: mt.ReadResourceRequestParams
    ) -> mt.ReadResourceRequestParams:
        uri = str(params.uri)
        if not _is_allowed(uri, self._resource_allow, self._resource_block):
            raise McpError(
                ErrorData(
                    code=_ERR_NOT_FOUND,
                    message=f"Resource blocked by policy: {uri}",
                )
            )
        return params

    async def on_list_resources(self, resources: list[Resource]) -> list[Resource]:
        return [
            r for r in resources
            if _is_allowed(str(r.uri), self._resource_allow, self._resource_block)
        ]

    # ------------------------------------------------------------------
    # Prompts
    # ------------------------------------------------------------------

    async def on_get_prompt_request(
        self, params: mt.GetPromptRequestParams
    ) -> mt.GetPromptRequestParams:
        if not _is_allowed(params.name, self._prompt_allow, self._prompt_block):
            raise McpError(
                ErrorData(
                    code=_ERR_NOT_FOUND,
                    message=f"Prompt blocked by policy: {params.name}",
                )
            )
        return params

    async def on_list_prompts(self, prompts: list[Prompt]) -> list[Prompt]:
        return [
            p for p in prompts
            if _is_allowed(p.name, self._prompt_allow, self._prompt_block)
        ]
