"""Adapts a list of PluginBase instances to a single fastmcp Middleware."""

from __future__ import annotations

from collections.abc import Sequence

import mcp.types as mt
from fastmcp.prompts.prompt import Prompt, PromptResult
from fastmcp.resources.resource import Resource, ResourceResult
from fastmcp.server.middleware.middleware import (
    CallNext,
    Middleware,
    MiddlewareContext,
)
from fastmcp.tools.tool import Tool, ToolResult

from .base import PluginBase


class PluginChainMiddleware(Middleware):
    """Adapts an ordered list of PluginBase instances to a fastmcp Middleware.

    All plugins for a server are collapsed into this single middleware object,
    avoiding deeply nested async call chains.

    Execution order for a request:
        plugin[0].request -> plugin[1].request -> ... -> upstream
        -> plugin[0].response -> plugin[1].response -> ...

    This gives plugin[0] the "outermost" position: it sees the request first
    and the response first.

    To block a request, a plugin raises ``mcp.McpError`` from its request hook.
    The error propagates naturally through fastmcp's error handling.
    """

    def __init__(self, plugins: list[PluginBase]) -> None:
        self._plugins = plugins

    # ------------------------------------------------------------------
    # Tool hooks
    # ------------------------------------------------------------------

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        params = context.message
        for plugin in self._plugins:
            params = await plugin.on_call_tool_request(params)
        result = await call_next(context.copy(message=params))
        for plugin in self._plugins:
            result = await plugin.on_call_tool_response(params, result)
        return result

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = list(await call_next(context))
        for plugin in self._plugins:
            tools = await plugin.on_list_tools(tools)
        return tools

    # ------------------------------------------------------------------
    # Resource hooks
    # ------------------------------------------------------------------

    async def on_read_resource(
        self,
        context: MiddlewareContext[mt.ReadResourceRequestParams],
        call_next: CallNext[mt.ReadResourceRequestParams, ResourceResult],
    ) -> ResourceResult:
        params = context.message
        for plugin in self._plugins:
            params = await plugin.on_read_resource_request(params)
        result = await call_next(context.copy(message=params))
        for plugin in self._plugins:
            result = await plugin.on_read_resource_response(params, result)
        return result

    async def on_list_resources(
        self,
        context: MiddlewareContext[mt.ListResourcesRequest],
        call_next: CallNext[mt.ListResourcesRequest, Sequence[Resource]],
    ) -> Sequence[Resource]:
        resources = list(await call_next(context))
        for plugin in self._plugins:
            resources = await plugin.on_list_resources(resources)
        return resources

    # ------------------------------------------------------------------
    # Prompt hooks
    # ------------------------------------------------------------------

    async def on_get_prompt(
        self,
        context: MiddlewareContext[mt.GetPromptRequestParams],
        call_next: CallNext[mt.GetPromptRequestParams, PromptResult],
    ) -> PromptResult:
        params = context.message
        for plugin in self._plugins:
            params = await plugin.on_get_prompt_request(params)
        result = await call_next(context.copy(message=params))
        for plugin in self._plugins:
            result = await plugin.on_get_prompt_response(params, result)
        return result

    async def on_list_prompts(
        self,
        context: MiddlewareContext[mt.ListPromptsRequest],
        call_next: CallNext[mt.ListPromptsRequest, Sequence[Prompt]],
    ) -> Sequence[Prompt]:
        prompts = list(await call_next(context))
        for plugin in self._plugins:
            prompts = await plugin.on_list_prompts(prompts)
        return prompts
