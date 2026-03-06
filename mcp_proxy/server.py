"""Build a composed FastMCP proxy server from a ProxyConfig."""

from __future__ import annotations

from fastmcp import FastMCP
from fastmcp.client.transports.http import StreamableHttpTransport
from fastmcp.client.transports.stdio import StdioTransport
from fastmcp.server import create_proxy

from .config.schema import (
    FilterPluginConfig,
    HttpTransportConfig,
    LoggingPluginConfig,
    PluginConfig,
    ProxyConfig,
    RewritePluginConfig,
    StdioTransportConfig,
    UpstreamConfig,
)
from .plugins.adapter import PluginChainMiddleware
from .plugins.base import PluginBase
from .plugins.filter_plugin import FilterPlugin
from .plugins.logging_plugin import JsonlLoggingPlugin
from .plugins.rewrite_plugin import RewritePlugin


def _build_plugin(config: PluginConfig) -> PluginBase:
    if isinstance(config, LoggingPluginConfig):
        return JsonlLoggingPlugin(config)
    elif isinstance(config, FilterPluginConfig):
        return FilterPlugin(config)
    elif isinstance(config, RewritePluginConfig):
        return RewritePlugin(config)
    raise ValueError(f"Unknown plugin type: {config.type}")  # type: ignore[union-attr]


def _build_plugin_chain(configs: list[PluginConfig]) -> PluginChainMiddleware:
    return PluginChainMiddleware([_build_plugin(c) for c in configs])


def _build_transport(upstream: UpstreamConfig) -> StdioTransport | StreamableHttpTransport:
    cfg = upstream.transport
    if isinstance(cfg, StdioTransportConfig):
        return StdioTransport(
            command=cfg.command,
            args=cfg.args,
            env=cfg.env or None,
            cwd=cfg.cwd,
        )
    elif isinstance(cfg, HttpTransportConfig):
        return StreamableHttpTransport(
            url=cfg.url,
            headers=cfg.headers or None,
        )
    raise ValueError(f"Unknown transport type: {cfg.type}")  # type: ignore[union-attr]


def build_server(config: ProxyConfig) -> FastMCP:
    """Construct the aggregating FastMCP proxy server from a ProxyConfig.

    Architecture:
    - A top-level ``FastMCP`` server acts as the aggregator.
    - Global plugins are attached to the aggregator as middleware (outermost layer).
    - Each upstream gets its own proxy sub-server (via ``create_proxy``).
    - Per-upstream plugins are attached to the sub-server before mounting.
    - Sub-servers are mounted onto the aggregator with their configured namespace.
    """
    main = FastMCP(config.proxy.name)

    if config.global_plugins:
        main.add_middleware(_build_plugin_chain(config.global_plugins))

    for upstream in config.upstreams:
        transport = _build_transport(upstream)
        sub = create_proxy(transport, name=upstream.name)

        if upstream.plugins:
            sub.add_middleware(_build_plugin_chain(upstream.plugins))

        main.mount(sub, namespace=upstream.namespace)

    return main


def run_server(config: ProxyConfig) -> None:
    """Build and run the proxy server according to config."""
    server = build_server(config)
    transport = config.proxy.transport
    if transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(
            transport=transport,
            host=config.proxy.host,
            port=config.proxy.port,
        )
