"""CLI entry point for mcp-proxy."""

from __future__ import annotations

from typing import Literal

import click

from .config.loader import load_config
from .server import run_server


@click.command()
@click.option(
    "--config",
    "-c",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Path to proxy YAML config file.",
)
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http", "streamable-http"]),
    default=None,
    help="Override the transport from the config file.",
)
@click.option(
    "--host",
    default=None,
    help="Override the host for HTTP transport.",
)
@click.option(
    "--port",
    default=None,
    type=int,
    help="Override the port for HTTP transport.",
)
def main(
    config: str,
    transport: str | None,
    host: str | None,
    port: int | None,
) -> None:
    """MCP Security Proxy — compose and filter one or more MCP servers.

    Reads a YAML config file that specifies upstream MCP servers and the
    plugin chain (logging, filtering, rewriting) to apply to each.

    Example (stdio, for Claude Desktop)::

        mcp-proxy --config proxy.yaml

    Example (HTTP server)::

        mcp-proxy --config proxy.yaml --transport http --port 8000
    """
    proxy_config = load_config(config)

    if transport is not None:
        proxy_config.proxy.transport = transport  # type: ignore[assignment]
    if host is not None:
        proxy_config.proxy.host = host
    if port is not None:
        proxy_config.proxy.port = port

    run_server(proxy_config)


if __name__ == "__main__":
    main()
