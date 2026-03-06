"""Pydantic models for the proxy YAML configuration."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Transport configs
# ---------------------------------------------------------------------------


class StdioTransportConfig(BaseModel):
    type: Literal["stdio"]
    command: str
    args: list[str] = []
    env: dict[str, str] = {}
    cwd: str | None = None


class HttpTransportConfig(BaseModel):
    type: Literal["http"]
    url: str
    headers: dict[str, str] = {}


TransportConfig = Annotated[
    StdioTransportConfig | HttpTransportConfig,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Plugin configs
# ---------------------------------------------------------------------------


class LoggingPluginConfig(BaseModel):
    type: Literal["logging"]
    log_file: str
    include_payloads: bool = True
    # If set, only log these MCP methods (e.g. ["tools/call", "resources/read"])
    methods: list[str] | None = None


class FilterPluginConfig(BaseModel):
    type: Literal["filter"]
    # Exactly one of allow_tools or block_tools may be set (validated below)
    allow_tools: list[str] | None = None
    block_tools: list[str] | None = None
    allow_resources: list[str] | None = None
    block_resources: list[str] | None = None
    allow_prompts: list[str] | None = None
    block_prompts: list[str] | None = None

    @model_validator(mode="after")
    def _check_allow_block_exclusive(self) -> FilterPluginConfig:
        if self.allow_tools is not None and self.block_tools is not None:
            raise ValueError("allow_tools and block_tools are mutually exclusive")
        if self.allow_resources is not None and self.block_resources is not None:
            raise ValueError("allow_resources and block_resources are mutually exclusive")
        if self.allow_prompts is not None and self.block_prompts is not None:
            raise ValueError("allow_prompts and block_prompts are mutually exclusive")
        return self


class RewritePluginConfig(BaseModel):
    type: Literal["rewrite"]
    # Map from upstream tool name -> exposed tool name
    tool_renames: dict[str, str] = {}
    # Map from upstream tool name -> dict of argument overrides (merged, overrides win)
    argument_overrides: dict[str, dict[str, Any]] = {}
    # Prepend this string to all text content blocks in tool responses
    response_prefix: str | None = None


PluginConfig = Annotated[
    LoggingPluginConfig | FilterPluginConfig | RewritePluginConfig,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Upstream and top-level configs
# ---------------------------------------------------------------------------


class UpstreamConfig(BaseModel):
    name: str
    transport: TransportConfig
    # Namespace prefix for all tools/resources/prompts from this upstream
    namespace: str | None = None
    plugins: list[PluginConfig] = []


class ProxyServerConfig(BaseModel):
    name: str = "mcp-proxy"
    transport: Literal["stdio", "http", "streamable-http"] = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000


class ProxyConfig(BaseModel):
    proxy: ProxyServerConfig = Field(default_factory=ProxyServerConfig)
    global_plugins: list[PluginConfig] = []
    upstreams: list[UpstreamConfig]
