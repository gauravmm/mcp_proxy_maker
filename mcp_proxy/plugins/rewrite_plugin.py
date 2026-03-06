"""Rewrite plugin: rename tools, inject arguments, prefix responses."""

from __future__ import annotations

from typing import Any

import mcp.types as mt
from fastmcp.tools.tool import Tool, ToolResult

from ..config.schema import RewritePluginConfig
from .base import PluginBase


class RewritePlugin(PluginBase):
    """Rename tools, inject fixed arguments, and prefix text responses.

    Tool renames are **symmetric**: the plugin maintains mappings in both
    directions so that:

    - ``on_call_tool_request``: translates the *exposed* name the client sends
      to the *upstream* name before forwarding.
    - ``on_list_tools``: translates the *upstream* name back to the *exposed*
      name in the listing returned to the client.

    Configuration uses upstream names as keys::

        tool_renames:
          read_file: read_document   # upstream "read_file" -> client sees "read_document"

    If the rewrite plugin is stacked with a filter plugin, the filter should
    use the **exposed** names (post-rename), since filter runs after rewrite
    in the chain and sees the list after renames have been applied.
    """

    def __init__(self, config: RewritePluginConfig) -> None:
        # upstream_name -> exposed_name  (used in on_list_tools)
        self._upstream_to_exposed: dict[str, str] = dict(config.tool_renames)
        # exposed_name -> upstream_name  (used in on_call_tool_request)
        self._exposed_to_upstream: dict[str, str] = {
            v: k for k, v in config.tool_renames.items()
        }
        # upstream_name -> {arg: value}
        self._arg_overrides: dict[str, dict[str, Any]] = dict(config.argument_overrides)
        self._response_prefix: str | None = config.response_prefix

    # ------------------------------------------------------------------
    # Tool hooks
    # ------------------------------------------------------------------

    async def on_call_tool_request(
        self, params: mt.CallToolRequestParams
    ) -> mt.CallToolRequestParams:
        upstream_name = self._exposed_to_upstream.get(params.name, params.name)
        overrides = self._arg_overrides.get(upstream_name, {})
        if upstream_name != params.name or overrides:
            new_args: dict[str, Any] = {**(params.arguments or {}), **overrides}
            return params.model_copy(
                update={"name": upstream_name, "arguments": new_args or None}
            )
        return params

    async def on_call_tool_response(
        self,
        params: mt.CallToolRequestParams,
        result: ToolResult,
    ) -> ToolResult:
        if not self._response_prefix:
            return result
        new_content = []
        for block in result.content:
            if hasattr(block, "text") and isinstance(block.text, str):
                new_content.append(
                    block.model_copy(
                        update={"text": self._response_prefix + block.text}
                    )
                )
            else:
                new_content.append(block)
        return result.model_copy(update={"content": new_content})

    async def on_list_tools(self, tools: list[Tool]) -> list[Tool]:
        result = []
        for tool in tools:
            exposed_name = self._upstream_to_exposed.get(tool.name, tool.name)
            if exposed_name != tool.name:
                result.append(tool.model_copy(update={"name": exposed_name}))
            else:
                result.append(tool)
        return result
