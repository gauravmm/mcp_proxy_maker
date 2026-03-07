# CLAUDE.md

## Commands

```bash
uv run pytest tests/ -v          # run all tests
uv run mcp-proxy --config examples/basic_proxy.yaml  # run proxy
uv sync --group dev              # install dev dependencies
```

Use `uv` for everything — not `python`/`pip` directly.

## Project Layout

```
src/mcp_proxy/
  config/schema.py      # Pydantic models (ProxyConfig, plugin configs, transport configs)
  config/loader.py      # YAML load + ${VAR} env expansion
  plugins/base.py       # PluginBase with pass-through defaults for all hooks
  plugins/adapter.py    # PluginChainMiddleware — bridges plugins to fastmcp Middleware
  plugins/filter_plugin.py   # Allow/block tools/resources/prompts by glob pattern
  plugins/rewrite_plugin.py  # Rename tools, inject args, prefix responses
  plugins/logging_plugin.py  # JSONL audit log
  plugins/inventory_plugin.py # JSON snapshot of available tools/resources/prompts
  server.py             # build_server() + run_server()
  cli.py                # Click CLI entry point
examples/               # basic_proxy.yaml, multi_upstream.yaml, security_filter.yaml
tests/                  # test_config.py, test_plugins.py
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
5. Add tests in `tests/test_plugins.py`

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
      - type: filter
        allow_tools: [str] | null    # glob patterns; mutually exclusive with block_tools
        block_tools: [str] | null
        allow_resources: [str] | null
        block_resources: [str] | null
        allow_prompts: [str] | null
        block_prompts: [str] | null
      - type: rewrite
        tool_renames: {upstream_name: exposed_name}
        argument_overrides: {upstream_name: {arg: value}}
        response_prefix: str | null
      - type: inventory
        inventory_file: str
```

## JSONL Log Schema (schema_version: 1)

```
ts, event (request/response/list), method, tool_name, resource_uri, prompt_name,
arguments, is_error, content_blocks, content_length_chars, duration_ms, items, item_count
```

## Key Constraints

- `allow_tools` and `block_tools` are mutually exclusive (validated by Pydantic)
- `tool_renames` keys are **upstream** names; values are **exposed** names
- `argument_overrides` keys are **upstream** names (pre-rename)
- If `filter` is stacked after `rewrite`, filter config uses **exposed** (post-rename) names
- Namespace separator is `_` (e.g. namespace `fs` + tool `read_file` → `fs_read_file`)
- Log files are opened in append mode at startup; no rotation in v1
