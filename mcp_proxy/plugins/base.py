"""Base plugin class with pass-through defaults for all hooks."""

from __future__ import annotations

from collections.abc import Sequence

import mcp.types as mt

from fastmcp.prompts.prompt import Prompt, PromptResult
from fastmcp.resources.resource import Resource, ResourceResult
from fastmcp.tools.tool import Tool, ToolResult


class PluginBase:
    """Base class for all proxy plugins.

    All methods pass through unchanged by default. Subclasses override only
    what they need to inspect or modify.

    To block an operation, raise ``mcp.McpError`` from any request hook.
    """

    # ------------------------------------------------------------------
    # Tool hooks
    # ------------------------------------------------------------------

    async def on_call_tool_request(
        self, params: mt.CallToolRequestParams
    ) -> mt.CallToolRequestParams:
        """Called before a tool call is forwarded to the upstream server.

        Raise ``McpError`` to block the call entirely.
        Return a (possibly modified) params object to continue.
        """
        return params

    async def on_call_tool_response(
        self,
        params: mt.CallToolRequestParams,
        result: ToolResult,
    ) -> ToolResult:
        """Called after a tool call response arrives from the upstream server."""
        return result

    async def on_list_tools(self, tools: list[Tool]) -> list[Tool]:
        """Called with the tool list returned by the upstream server.

        Return a filtered or modified list.
        """
        return tools

    # ------------------------------------------------------------------
    # Resource hooks
    # ------------------------------------------------------------------

    async def on_read_resource_request(
        self, params: mt.ReadResourceRequestParams
    ) -> mt.ReadResourceRequestParams:
        """Called before a resource read is forwarded to the upstream server."""
        return params

    async def on_read_resource_response(
        self,
        params: mt.ReadResourceRequestParams,
        result: ResourceResult,
    ) -> ResourceResult:
        """Called after a resource read response arrives from the upstream server."""
        return result

    async def on_list_resources(
        self, resources: list[Resource]
    ) -> list[Resource]:
        """Called with the resource list returned by the upstream server."""
        return resources

    # ------------------------------------------------------------------
    # Prompt hooks
    # ------------------------------------------------------------------

    async def on_get_prompt_request(
        self, params: mt.GetPromptRequestParams
    ) -> mt.GetPromptRequestParams:
        """Called before a prompt get is forwarded to the upstream server."""
        return params

    async def on_get_prompt_response(
        self,
        params: mt.GetPromptRequestParams,
        result: PromptResult,
    ) -> PromptResult:
        """Called after a prompt get response arrives from the upstream server."""
        return result

    async def on_list_prompts(self, prompts: list[Prompt]) -> list[Prompt]:
        """Called with the prompt list returned by the upstream server."""
        return prompts
