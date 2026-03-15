"""Build a composed FastMCP proxy server from a ProxyConfig."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import anyio
import httpx
from fastmcp import Client, FastMCP
from fastmcp.client.auth import OAuth
from fastmcp.client.transports.http import StreamableHttpTransport
from fastmcp.client.transports.stdio import StdioTransport
from fastmcp.server import create_proxy
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)

from .config.schema import (
    FilterPluginConfig,
    HttpTransportConfig,
    InventoryPluginConfig,
    LoggingPluginConfig,
    NotionAccessPluginConfig,
    OAuthConfig,
    PluginConfig,
    ProxyConfig,
    RewritePluginConfig,
    StdioTransportConfig,
    UpstreamConfig,
)
from .plugins.adapter import PluginChainMiddleware
from .plugins.base import PluginBase
from .plugins.filter_plugin import FilterPlugin
from .plugins.inventory_plugin import InventoryPlugin
from .plugins.logging_plugin import JsonlLoggingPlugin
from .plugins.notion_access_plugin import NotionAccessPlugin
from .plugins.rewrite_plugin import RewritePlugin


def _build_oauth(cfg: OAuthConfig, upstream_name: str) -> OAuth:
    storage_dir = (Path(".oauth2") / upstream_name).resolve()
    storage_dir.mkdir(parents=True, exist_ok=True)
    store = FileTreeStore(
        data_directory=storage_dir,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(storage_dir),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(storage_dir),
    )
    return OAuth(
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
        scopes=cfg.scopes,
        token_storage=store,
        callback_port=cfg.callback_port or 53997,
    )


def _build_plugin(config: PluginConfig) -> PluginBase:
    if isinstance(config, LoggingPluginConfig):
        return JsonlLoggingPlugin(config)
    elif isinstance(config, FilterPluginConfig):
        return FilterPlugin(config)
    elif isinstance(config, RewritePluginConfig):
        return RewritePlugin(config)
    elif isinstance(config, InventoryPluginConfig):
        return InventoryPlugin(config)
    elif isinstance(config, NotionAccessPluginConfig):
        return NotionAccessPlugin(config)
    raise ValueError(f"Unknown plugin type: {config.type}")  # type: ignore[union-attr]


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
        auth = _build_oauth(cfg.oauth, upstream.name) if cfg.oauth is not None else None

        def http2_client_factory(
            headers: dict | None = None,
            timeout: httpx.Timeout | None = None,
            auth: httpx.Auth | None = None,
            **_kwargs,
        ) -> httpx.AsyncClient:
            from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT

            return httpx.AsyncClient(
                headers=headers or {},
                timeout=timeout
                or httpx.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT),
                auth=auth,
                follow_redirects=True,
                http2=True,
            )

        return StreamableHttpTransport(
            url=cfg.url,
            headers=cfg.headers or None,
            auth=auth,
            httpx_client_factory=http2_client_factory,
        )
    raise ValueError(f"Unknown transport type: {cfg.type}")  # type: ignore[union-attr]


def build_server(
    config: ProxyConfig,
    connected_clients: dict[str, Client] | None = None,
) -> FastMCP:
    """Construct the aggregating FastMCP proxy server from a ProxyConfig.

    Architecture:
    - A top-level ``FastMCP`` server acts as the aggregator.
    - Global plugins are attached to the aggregator as middleware (outermost layer).
    - Each upstream gets its own proxy sub-server (via ``create_proxy``).
    - Per-upstream plugins are attached to the sub-server before mounting.
    - Sub-servers are mounted onto the aggregator with their configured namespace.
    - After building the chain, each plugin's ``register_tools`` is called on the
      aggregator so plugins can inject synthetic tools alongside the proxied ones.

    If ``connected_clients`` is provided, HTTP upstreams with ``persistent_connection``
    enabled will use those pre-connected clients instead of creating fresh sessions per
    request.
    """
    main = FastMCP(config.proxy.name)

    if config.global_plugins:
        plugins = [_build_plugin(c) for c in config.global_plugins]
        main.add_middleware(PluginChainMiddleware(plugins))
        for p in plugins:
            p.register_tools(main)

    for upstream in config.upstreams:
        # Use a pre-connected client if available (persistent_connection=true), else transport.
        target: Any
        if connected_clients and upstream.name in connected_clients:
            target = connected_clients[upstream.name]
        else:
            target = _build_transport(upstream)
        sub = create_proxy(target, name=upstream.name)

        if upstream.plugins:
            plugins = [_build_plugin(c) for c in upstream.plugins]
            sub.add_middleware(PluginChainMiddleware(plugins))
            for p in plugins:
                p.register_tools(main)

        main.mount(sub, namespace=upstream.namespace)

    return main


async def _run_server_async(config: ProxyConfig) -> None:
    """Async entry point: connects persistent clients, builds, and runs the server."""
    async with contextlib.AsyncExitStack() as stack:
        connected_clients: dict[str, Client] = {}
        for upstream in config.upstreams:
            if (
                isinstance(upstream.transport, HttpTransportConfig)
                and upstream.transport.persistent_connection
            ):
                transport = _build_transport(upstream)
                client: Client = await stack.enter_async_context(Client(transport))
                connected_clients[upstream.name] = client

        server = build_server(config, connected_clients=connected_clients or None)
        transport = config.proxy.transport
        if transport == "stdio":
            await server.run_async(transport="stdio")
        else:
            await server.run_async(
                transport=transport,
                host=config.proxy.host,
                port=config.proxy.port,
            )


def run_server(config: ProxyConfig) -> None:
    """Build and run the proxy server according to config."""
    anyio.run(_run_server_async, config)
