"""Inventory plugin: writes a clean JSON snapshot of available tools/resources/prompts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastmcp.prompts.prompt import Prompt
from fastmcp.resources.resource import Resource
from fastmcp.tools.tool import Tool

from ..config.schema import InventoryPluginConfig
from .base import PluginBase


class InventoryPlugin(PluginBase):
    """Maintains a JSON file with the latest known inventory of tools, resources, and prompts.

    The file is rewritten each time a list hook fires, so it always reflects
    the most recent state reported by the upstream server.
    """

    def __init__(self, config: InventoryPluginConfig) -> None:
        self._path = Path(config.inventory_file)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._tools: list[dict[str, Any]] | None = None
        self._resources: list[dict[str, Any]] | None = None
        self._prompts: list[dict[str, Any]] | None = None

    def _write_snapshot(self) -> None:
        snapshot: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        }
        if self._tools is not None:
            snapshot["tools"] = self._tools
        if self._resources is not None:
            snapshot["resources"] = self._resources
        if self._prompts is not None:
            snapshot["prompts"] = self._prompts
        self._path.write_text(json.dumps(snapshot, indent=2, default=str) + "\n")

    async def on_list_tools(self, tools: list[Tool]) -> list[Tool]:
        self._tools = [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            }
            for t in tools
        ]
        self._write_snapshot()
        return tools

    async def on_list_resources(self, resources: list[Resource]) -> list[Resource]:
        self._resources = [
            {
                "uri": str(r.uri),
                "name": r.name,
                "description": r.description,
                "mime_type": r.mime_type,
            }
            for r in resources
        ]
        self._write_snapshot()
        return resources

    async def on_list_prompts(self, prompts: list[Prompt]) -> list[Prompt]:
        self._prompts = [
            {
                "name": p.name,
                "description": p.description,
            }
            for p in prompts
        ]
        self._write_snapshot()
        return prompts
