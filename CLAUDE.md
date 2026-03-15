# CLAUDE.md

## Overview

This is an MCP security proxy with built-in Claude Code skills. The intended workflow is:

1. Point the proxy at an upstream MCP server with logging + inventory plugins enabled.
2. Run `/probe-mcp` — Claude probes the server interactively, testing tools and identifying security issues.
3. Run `/propose-filters` — Claude analyzes logs and proposes mitigations, from YAML config to custom content-aware plugins. Claude writes the plugin code, config, and tests.

The proxy and plugin system provide the runtime; Claude handles analysis and code generation.

## Commands

```bash
uv run pytest tests/ -v          # run all tests
uv run mcp-proxy --config examples/basic_proxy.yaml  # run proxy
uv sync --group dev              # install dev dependencies
```

Use `uv` for everything — not `python`/`pip` directly.

## Project Layout

```
mcp_proxy/
  config/schema.py      # Pydantic models (ProxyConfig, plugin configs, transport configs)
  config/loader.py      # YAML load + ${VAR} env expansion
  plugins/base.py       # PluginBase with pass-through defaults for all hooks
  plugins/adapter.py    # PluginChainMiddleware — bridges plugins to fastmcp Middleware
  plugins/filter_plugin.py        # Allow/block tools/resources/prompts by glob pattern
  plugins/rewrite_plugin.py       # Rename tools, inject args, prefix responses
  plugins/logging_plugin.py       # JSONL audit log
  plugins/inventory_plugin.py     # JSON snapshot of available tools/resources/prompts
  plugins/notion_access_plugin.py # Per-bot page-level access control for Notion upstreams
  plugins/hive_access_plugin.py   # Workspace + project scope enforcement for Hive upstreams
  server.py             # build_server() + run_server()
  cli.py                # Click CLI entry point
examples/               # basic_proxy.yaml, multi_upstream.yaml, security_filter.yaml
tests/                  # test_config.py, test_plugins.py, test_notion_access_plugin.py,
                        # test_hive_access_plugin.py
.claude/skills/
  probe-mcp.md          # /probe-mcp — interactive security probing
  propose-filters.md    # /propose-filters — analyze logs, propose & build mitigations
```

## Architecture

**fastmcp** (3.1+, Python 3.14) handles the MCP protocol. The proxy uses:

- `create_proxy(transport)` — creates a FastMCP sub-server that proxies an upstream
- `main.mount(sub, namespace=...)` — aggregates sub-servers into one endpoint
- `Middleware` subclass with hooks per MCP method

**Plugin system:**

- `PluginBase` — base class; all hooks are pass-through by default
- `PluginChainMiddleware(plugins)` — a single fastmcp `Middleware` that calls each plugin in order
- Request hooks run plugin[0] → plugin[1] → ... → upstream
- Response hooks run plugin[0] → plugin[1] → ... (same forward order)
- Blocking: raise `McpError(ErrorData(code=-32601, message="..."))` from any request hook
- Hiding: set `hide_blocked = True` (default) and override `is_tool_allowed` / `is_resource_allowed` / `is_prompt_allowed`; the adapter auto-filters list responses so you only declare the policy once
- Upstream client access: override `set_upstream_client(client)` to receive a `fastmcp.Client` for the upstream; `build_server()` calls this automatically on every plugin when `persistent_connection: true` is set on the transport. Use it to make out-of-band verification calls from within a hook.

**Middleware context mutation:**
`MiddlewareContext` is a frozen dataclass. To pass modified params to `call_next`:

```python
await call_next(context.copy(message=new_params))
```

**Available hooks in `PluginChainMiddleware`:**
`on_call_tool`, `on_list_tools`, `on_read_resource`, `on_list_resources`, `on_get_prompt`, `on_list_prompts`

## Adding a New Plugin

1. Add a config model in `config/schema.py` with `type: Literal["yourtype"]`
2. Add it to the `PluginConfig` discriminated union
3. Subclass `PluginBase` in `plugins/your_plugin.py`, override only the hooks you need
4. Add a branch in `server._build_plugin()` to instantiate it from config
5. Add tests in `tests/test_your_plugin.py`
6. If the plugin needs to call the upstream (e.g. for verification): override `set_upstream_client(client)` to store the client; ensure the upstream uses `persistent_connection: true`

## Config Schema Summary

```yaml
proxy:
  name: str              # default: "mcp-proxy"
  transport: stdio|http|streamable-http   # default: stdio
  host: str              # default: "127.0.0.1" (HTTP only)
  port: int              # default: 8000 (HTTP only)

global_plugins: [...]    # applied to all upstreams

upstreams:
  - name: str
    namespace: str | null
    transport:
      type: stdio
      command: str
      args: [str]
      env: {str: str}    # supports ${ENV_VAR} expansion
      cwd: str | null
    # OR
    transport:
      type: http
      url: str
      headers: {str: str}
    plugins:
      - type: logging
        log_file: str
        include_payloads: bool   # default: true
        methods: [str] | null    # default: all
        max_bytes: int | null    # default: null (no rotation)
        max_backups: int         # default: 5
      - type: filter
        allow_tools: [str] | null    # glob patterns; mutually exclusive with block_tools
        block_tools: [str] | null
        allow_resources: [str] | null
        block_resources: [str] | null
        allow_prompts: [str] | null
        block_prompts: [str] | null
        hide_blocked: bool           # default: true — hide blocked items from list responses
      - type: rewrite
        tool_renames: {upstream_name: exposed_name}
        argument_overrides: {upstream_name: {arg: value}}
        response_prefix: str | null
      - type: inventory
        inventory_file: str
      - type: hive_access
        workspace_id: str           # injected into all tools accepting workspaceId
        allowed_project_ids: [str]  # agent may use a subset; never a superset
        hide_blocked: bool          # default: true
```

## JSONL Log Schema (schema_version: 2)

Calls are logged as a single paired entry after the response is received.

```
ts, method, tool_name, resource_uri, prompt_name,
arguments, is_error, content_blocks, content_length_chars, duration_ms, items, item_count
```

## Skills

### `/probe-mcp`

Probes an MCP server through the proxy. Discovers tools/resources/prompts, tests them with safe inputs, then (with user approval) tests for SSRF, path traversal, scheme abuse, etc. Outputs a structured probe report to `logs/probe_report.md`.

### `/propose-filters`

Analyzes probe reports and audit logs to propose mitigations at three levels:

- **Level 1**: YAML `filter` config (allow/block by glob pattern)
- **Level 2**: YAML `rewrite` config (lock down arguments)
- **Level 3**: Custom `PluginBase` subclass (content-aware inspection of arguments or responses — e.g. URL domain allowlists, PII redaction, metadata gates)

For Level 3, Claude writes the full plugin: config model in `schema.py`, plugin class, server wiring, and tests.

## Hive Access Plugin Notes

- Always stack `filter` before `hive_access` in the plugin list — filter reduces ~84 tools to ~15, hive_access then scopes those.
- `persistent_connection: true` is required on the HTTP transport for write-tool action verification (enables `set_upstream_client`).
- The action→project cache has no TTL (actions are immutable w.r.t. project). It is populated from every `getActions` response.
- `getActions` with `projectIds: null` (explicit null, not absent) is blocked — it would return project-less actions outside the allowlist.
- `insertActions` checks `projectId` per action in the request payload; the upstream never sees a disallowed project ID.
- Write tools (`updateActions*`) that take `actionIds[]` without a `projectId` are verified via the cache + upstream fallback. If an ID can't be resolved after the fallback fetch, the call is blocked conservatively.

## Key Constraints

- `allow_tools` and `block_tools` are mutually exclusive (validated by Pydantic)
- `tool_renames` keys are **upstream** names; values are **exposed** names
- `argument_overrides` keys are **upstream** names (pre-rename)
- If `filter` is stacked after `rewrite`, filter config uses **exposed** (post-rename) names
- Namespace separator is `_` (e.g. namespace `fs` + tool `read_file` → `fs_read_file`)
- Log files are opened in append mode at startup
- Log rotation is opt-in via `max_bytes`; backups use numeric suffixes (`.1`, `.2`, ...)
- `hide_blocked: true` (default) removes blocked items from `list_tools/resources/prompts`; set `false` for transparency (items visible in listings but calls still rejected)
