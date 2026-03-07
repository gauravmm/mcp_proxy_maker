"""Logging plugin: structured JSONL log of all MCP operations."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import mcp.types as mt
from fastmcp.prompts.prompt import Prompt, PromptResult
from fastmcp.resources.resource import Resource, ResourceResult
from fastmcp.tools.tool import Tool, ToolResult

from ..config.schema import LoggingPluginConfig
from .base import PluginBase


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text_length(blocks: list[Any]) -> int:
    """Total character count across all text content blocks."""
    total = 0
    for b in blocks:
        if hasattr(b, "text") and isinstance(b.text, str):
            total += len(b.text)
    return total


class JsonlLoggingPlugin(PluginBase):
    """Append-mode JSONL log of all MCP proxy operations.

    Each line is a self-contained JSON object.  See the schema in the project
    README for field descriptions.

    The log file is opened at construction time (append mode).  Optional
    size-based rotation: when ``max_bytes`` is set, the file is rotated when
    it exceeds that size.  Old files are renamed with numeric suffixes
    (``.1``, ``.2``, ...) up to ``max_backups``.
    """

    def __init__(self, config: LoggingPluginConfig) -> None:
        self._include_payloads = config.include_payloads
        self._methods: set[str] | None = set(config.methods) if config.methods else None
        self._path = Path(config.log_file)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._max_bytes = config.max_bytes
        self._max_backups = config.max_backups
        self._file = open(self._path, "a", encoding="utf-8")  # noqa: SIM115

    def _should_log(self, method: str) -> bool:
        return self._methods is None or method in self._methods

    def _rotate(self) -> None:
        """Rotate log files: current -> .1, .1 -> .2, ..., delete oldest."""
        self._file.close()
        for i in range(self._max_backups, 0, -1):
            src = self._path.with_suffix(f"{self._path.suffix}.{i}")
            if i == self._max_backups:
                src.unlink(missing_ok=True)
            else:
                dst = self._path.with_suffix(f"{self._path.suffix}.{i + 1}")
                if src.exists():
                    src.rename(dst)
        self._path.rename(self._path.with_suffix(f"{self._path.suffix}.1"))
        self._file = open(self._path, "a", encoding="utf-8")  # noqa: SIM115

    def _write(self, entry: dict[str, Any]) -> None:
        self._file.write(json.dumps(entry, default=str) + "\n")
        self._file.flush()
        if self._max_bytes is not None and self._path.stat().st_size >= self._max_bytes:
            self._rotate()

    def _base(self, method: str) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "ts": _now_iso(),
            "method": method,
        }

    # ------------------------------------------------------------------
    # Tool hooks
    # ------------------------------------------------------------------

    async def on_call_tool_request(
        self, params: mt.CallToolRequestParams
    ) -> mt.CallToolRequestParams:
        # Stash start time for duration calculation in the response hook.
        params.__pydantic_extra__ = params.__pydantic_extra__ or {}
        params.__pydantic_extra__["_proxy_t0"] = time.monotonic()
        return params

    async def on_call_tool_response(
        self,
        params: mt.CallToolRequestParams,
        result: ToolResult,
    ) -> ToolResult:
        if self._should_log("tools/call"):
            t0 = (params.__pydantic_extra__ or {}).get("_proxy_t0")
            duration_ms = (time.monotonic() - t0) * 1000 if t0 is not None else None
            entry = self._base("tools/call")
            entry["tool_name"] = params.name
            entry["arguments"] = params.arguments if self._include_payloads else None
            entry["is_error"] = any(getattr(b, "type", None) == "error" for b in result.content)
            entry["content_blocks"] = len(result.content)
            entry["content_length_chars"] = _text_length(result.content)
            entry["duration_ms"] = round(duration_ms, 2) if duration_ms else None
            if self._include_payloads:
                entry["response_payload"] = " ".join(
                    b.text for b in result.content if hasattr(b, "text") and isinstance(b.text, str)
                )
            self._write(entry)
        return result

    async def on_list_tools(self, tools: list[Tool]) -> list[Tool]:
        if self._should_log("tools/list"):
            entry = self._base("tools/list")
            entry["item_count"] = len(tools)
            entry["items"] = [t.name for t in tools] if self._include_payloads else None
            self._write(entry)
        return tools

    # ------------------------------------------------------------------
    # Resource hooks
    # ------------------------------------------------------------------

    async def on_read_resource_request(
        self, params: mt.ReadResourceRequestParams
    ) -> mt.ReadResourceRequestParams:
        params.__pydantic_extra__ = params.__pydantic_extra__ or {}
        params.__pydantic_extra__["_proxy_t0"] = time.monotonic()
        return params

    async def on_read_resource_response(
        self,
        params: mt.ReadResourceRequestParams,
        result: ResourceResult,
    ) -> ResourceResult:
        if self._should_log("resources/read"):
            t0 = (params.__pydantic_extra__ or {}).get("_proxy_t0")
            duration_ms = (time.monotonic() - t0) * 1000 if t0 is not None else None
            entry = self._base("resources/read")
            entry["resource_uri"] = str(params.uri)
            entry["duration_ms"] = round(duration_ms, 2) if duration_ms else None
            self._write(entry)
        return result

    async def on_list_resources(self, resources: list[Resource]) -> list[Resource]:
        if self._should_log("resources/list"):
            entry = self._base("resources/list")
            entry["item_count"] = len(resources)
            entry["items"] = [str(r.uri) for r in resources] if self._include_payloads else None
            self._write(entry)
        return resources

    # ------------------------------------------------------------------
    # Prompt hooks
    # ------------------------------------------------------------------

    async def on_get_prompt_request(
        self, params: mt.GetPromptRequestParams
    ) -> mt.GetPromptRequestParams:
        params.__pydantic_extra__ = params.__pydantic_extra__ or {}
        params.__pydantic_extra__["_proxy_t0"] = time.monotonic()
        return params

    async def on_get_prompt_response(
        self,
        params: mt.GetPromptRequestParams,
        result: PromptResult,
    ) -> PromptResult:
        if self._should_log("prompts/get"):
            t0 = (params.__pydantic_extra__ or {}).get("_proxy_t0")
            duration_ms = (time.monotonic() - t0) * 1000 if t0 is not None else None
            entry = self._base("prompts/get")
            entry["prompt_name"] = params.name
            entry["arguments"] = params.arguments if self._include_payloads else None
            entry["duration_ms"] = round(duration_ms, 2) if duration_ms else None
            self._write(entry)
        return result

    async def on_list_prompts(self, prompts: list[Prompt]) -> list[Prompt]:
        if self._should_log("prompts/list"):
            entry = self._base("prompts/list")
            entry["item_count"] = len(prompts)
            entry["items"] = [p.name for p in prompts] if self._include_payloads else None
            self._write(entry)
        return prompts
