"""Logging plugin: structured JSONL log of all MCP operations."""

from __future__ import annotations

import json
import shutil
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


def _extract_response(blocks: list[Any]) -> tuple[str, list[Any]]:
    """Split content blocks into (text_payload, binary_blocks).

    ``text_payload`` is the joined text from all TextContent blocks.
    ``binary_blocks`` is the list of all non-text blocks (images, audio, etc.).
    """
    texts: list[str] = []
    binary: list[Any] = []
    for b in blocks:
        if hasattr(b, "text") and isinstance(b.text, str):
            texts.append(b.text)
        else:
            binary.append(b)
    return " ".join(texts), binary


class JsonlLoggingPlugin(PluginBase):
    """Append-mode JSONL log of all MCP proxy operations.

    Each line is a self-contained JSON object.  See the schema in the project
    README for field descriptions.

    The log file is opened at construction time (append mode).  Optional
    size-based rotation: when ``max_bytes`` is set, the file is rotated when
    it exceeds that size.  Old files are renamed with numeric suffixes
    (``.1``, ``.2``, ...) up to ``max_backups``.

    Large response payloads (beyond ``payload_offload_chars``) are written to
    sidecar files in ``{log_stem}_payloads/`` instead of being inlined in the
    JSONL.  Sidecar directories are rotated and deleted alongside their
    matching log backup.

    Binary content blocks (images, audio) are omitted by default
    (``include_binary_payloads=False``).  Set to ``True`` to write them to
    sidecar ``.json`` files instead.
    """

    def __init__(self, config: LoggingPluginConfig) -> None:
        self._include_payloads = config.include_payloads
        self._methods: set[str] | None = set(config.methods) if config.methods else None
        self._path = Path(config.log_file)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._max_bytes = config.max_bytes
        self._max_backups = config.max_backups
        self._payload_offload_chars = config.payload_offload_chars
        self._include_binary_payloads = config.include_binary_payloads
        self._payload_seq = 0
        self._file = open(self._path, "a", encoding="utf-8")  # noqa: SIM115

    # ------------------------------------------------------------------
    # Payload sidecar helpers
    # ------------------------------------------------------------------

    def _payloads_dir(self) -> Path:
        return self._path.with_name(f"{self._path.stem}_payloads")

    def _backup_payloads_dir(self, n: int) -> Path:
        return self._path.with_name(f"{self._path.stem}_payloads.{n}")

    def _next_payload_path(self, label: str, ext: str) -> Path:
        self._payload_seq += 1
        safe = "".join(c if c.isalnum() or c == "_" else "_" for c in label)[:32]
        return self._payloads_dir() / f"{self._payload_seq:05d}_{safe}{ext}"

    def _offload_text(self, text: str, label: str) -> str:
        """Write *text* to a sidecar ``.txt`` file; return relative path."""
        path = self._next_payload_path(label, ".txt")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return str(path.relative_to(self._path.parent))

    def _offload_binary(self, blocks: list[Any], label: str) -> str:
        """Serialize binary blocks to a sidecar ``.json`` file; return relative path."""
        path = self._next_payload_path(label, ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = []
        for b in blocks:
            block: dict[str, Any] = {"type": str(getattr(b, "type", "unknown"))}
            if hasattr(b, "mimeType"):
                block["mimeType"] = b.mimeType
            if hasattr(b, "data"):
                block["data"] = b.data
            data.append(block)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return str(path.relative_to(self._path.parent))

    # ------------------------------------------------------------------
    # Core write / rotate
    # ------------------------------------------------------------------

    def _should_log(self, method: str) -> bool:
        return self._methods is None or method in self._methods

    def _rotate(self) -> None:
        """Rotate log and payload sidecar dirs: current -> .1, ..., delete oldest."""
        self._file.close()
        for i in range(self._max_backups, 0, -1):
            src = self._path.with_suffix(f"{self._path.suffix}.{i}")
            if i == self._max_backups:
                src.unlink(missing_ok=True)
                pd = self._backup_payloads_dir(i)
                if pd.exists():
                    shutil.rmtree(pd)
            else:
                dst = self._path.with_suffix(f"{self._path.suffix}.{i + 1}")
                if src.exists():
                    src.rename(dst)
                src_pd = self._backup_payloads_dir(i)
                dst_pd = self._backup_payloads_dir(i + 1)
                if src_pd.exists():
                    src_pd.rename(dst_pd)
        self._path.rename(self._path.with_suffix(f"{self._path.suffix}.1"))
        cur_pd = self._payloads_dir()
        if cur_pd.exists():
            cur_pd.rename(self._backup_payloads_dir(1))
        self._file = open(self._path, "a", encoding="utf-8")  # noqa: SIM115
        self._payload_seq = 0

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

            text_payload, binary_blocks = _extract_response(result.content)

            entry = self._base("tools/call")
            entry["tool_name"] = params.name
            entry["arguments"] = params.arguments if self._include_payloads else None
            entry["is_error"] = any(getattr(b, "type", None) == "error" for b in result.content)
            entry["content_blocks"] = len(result.content)
            entry["content_length_chars"] = len(text_payload)
            entry["duration_ms"] = round(duration_ms, 2) if duration_ms else None

            if binary_blocks:
                entry["binary_content"] = True

            if self._include_payloads:
                # Binary blocks
                if binary_blocks and self._include_binary_payloads:
                    entry["binary_payload_file"] = self._offload_binary(binary_blocks, params.name)
                # Text payload: inline or offloaded
                if text_payload:
                    if (
                        self._payload_offload_chars is not None
                        and len(text_payload) > self._payload_offload_chars
                    ):
                        entry["payload_file"] = self._offload_text(text_payload, params.name)
                    else:
                        entry["response_payload"] = text_payload

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
