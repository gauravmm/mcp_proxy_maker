# MCP Security Proxy

A configurable proxy for [Model Context Protocol (MCP)](https://modelcontextprotocol.io) servers. Sits between an MCP client (e.g. Claude Desktop) and one or more upstream MCP servers, applying a plugin pipeline for logging, filtering, and rewriting requests and responses.

As an MCP proxy, it has all the features you'd expect. The real strength of this comes when you use it with the Claude Code skills to generate better filters.

**Intended workflow:** Point the proxy at an MCP server, then use the built-in Claude Code skills (`/probe-mcp` and `/propose-filters`) to have Claude analyze the server's security surface and generate appropriate mitigations — from simple YAML config to custom content-aware plugins. The proxy and plugins provide the runtime machinery; Claude does the heavy lifting of analysis and code generation.

If your server is sensitive or has production data (testing in production? _really?_), you could just use the MCP proxy to generate logs. Once you have enough logs, run `/propose-filters` and Claude will do just that. If there are gaps in your logs, Claude will try to identify them so you can generate more logs and close the gap.

## Quick Start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). You'll be making plenty of changes to the codebase, so clone it for yourself.

```bash
git clone <repo>
cd mcp-security-proxy-maker

uv run mcp-proxy --config examples/basic_proxy.yaml
```

Then reopen Claude Code to this repository so it picks up the `.mcp.json` file and connects to the server. Each time you restart the server, re-run `/mcp` and reconnect to the MCP server.

## Configuration

The config file is YAML. String values support `${ENV_VAR}` expansion.

```yaml
proxy:
  name: my-proxy          # Name advertised to MCP clients
  transport: stdio        # "stdio" | "http" | "streamable-http"
  host: "127.0.0.1"       # HTTP only
  port: 8000              # HTTP only

global_plugins:           # Applied to all upstreams (outermost layer)
  - type: logging
    log_file: logs/all.jsonl

upstreams:
  - name: filesystem
    namespace: fs         # Tools exposed as "fs_read_file", etc.
    transport:
      type: stdio
      command: uvx
      args: ["mcp-server-filesystem", "/home/user/docs"]
      env:
        SOME_VAR: "${ENV_VALUE}"
      cwd: /optional/working/dir
    plugins:
      - type: filter
        block_tools: ["write_*", "delete_*"]
      - type: rewrite
        tool_renames:
          read_file: read_document
        argument_overrides:
          read_file:
            encoding: "utf-8"
      - type: logging
        log_file: logs/fs.jsonl
      - type: inventory
        inventory_file: logs/fs_inventory.json

  - name: remote
    namespace: api
    transport:
      type: http
      url: "https://example.com/mcp"
      headers:
        Authorization: "Bearer ${API_TOKEN}"
    plugins:
      - type: filter
        allow_tools: ["search", "get_*"]

  - name: notion
    transport:
      type: http
      url: "https://mcp.notion.com/mcp"
      oauth:
        client_id: "${NOTION_CLIENT_ID}"
        client_secret: "${NOTION_CLIENT_SECRET}"
        scopes: []
```

### OAuth 2.0

HTTP upstreams can use OAuth instead of (or in addition to) static headers. Add an `oauth` block to the transport config with `client_id`, `client_secret`, and `scopes`. Tokens are persisted to `.oauth2/<upstream_name>/` so they survive proxy restarts. On first connection, the proxy will open a browser for the OAuth authorization flow.

All three fields are optional and default to `null` -- the upstream server's OAuth discovery endpoint determines what's required.

### Plugin execution order

Plugins run in the order listed. For a request, plugin[0] runs first; for the response, plugin[0] also runs first (it sees the response before plugin[1] does). The global plugins wrap the per-upstream plugins, so global runs outermost.

## Plugins

### `logging`

Appends one JSON line per operation to a file.

| Field | Default | Description |
|---|---|---|
| `log_file` | required | Path to the JSONL output file. Parent dirs are created automatically. |
| `include_payloads` | `true` | Include request arguments and response text in log entries. |
| `methods` | all | Limit logging to specific MCP methods, e.g. `["tools/call"]`. |
| `max_bytes` | none | Rotate the log file when it exceeds this size in bytes. No rotation if unset. |
| `max_backups` | `5` | Number of rotated backup files to keep (`.1`, `.2`, ...). |

**Log entry fields:**

Each call (tool/resource/prompt) is logged as a single paired entry after the response is received, combining request and response data.

| Field | Description |
|---|---|
| `schema_version` | Always `2` |
| `ts` | ISO 8601 UTC timestamp (of response) |
| `method` | MCP method (`tools/call`, `resources/read`, etc.) |
| `tool_name` | Tool name (tool operations only) |
| `resource_uri` | Resource URI (resource operations only) |
| `prompt_name` | Prompt name (prompt operations only) |
| `arguments` | Request arguments (null if `include_payloads: false`) |
| `is_error` | Whether the response was an error (tool calls only) |
| `content_blocks` | Number of content blocks in response (tool calls only) |
| `content_length_chars` | Total text length of response (tool calls only) |
| `duration_ms` | Round-trip time in milliseconds |
| `items` | List of tool/resource/prompt names (list events only) |
| `item_count` | Count of items (list events only) |

### `filter`

Blocks or hides tools, resources, and prompts. Policy is enforced in both the listing (hidden from `tools/list`) and at call time (raises an error).

Two mutually exclusive modes per category:

- **Allow-list** (`allow_tools`): only matching names are accessible; everything else is blocked.
- **Deny-list** (`block_tools`): matching names are blocked; everything else passes through.

Values are [glob patterns](https://docs.python.org/3/library/fnmatch.html) (`*` matches anything within a name, `?` matches one character).

```yaml
- type: filter
  allow_tools: ["read_*", "list_*"]    # allow-list mode
  block_resources: ["secret://*"]      # deny-list mode for resources
  block_prompts: ["admin_*"]
```

### `rewrite`

Modifies tool names and call arguments. All renames are symmetric: the plugin translates in both directions so the client always sees the exposed name.

```yaml
- type: rewrite
  tool_renames:
    upstream_name: exposed_name    # upstream -> what client sees
  argument_overrides:
    upstream_name:                 # keyed by upstream name
      arg_key: forced_value        # merged after user args; overrides win
  response_prefix: "Source: "     # prepended to all text content blocks
```

**Note:** If a `filter` plugin is stacked after a `rewrite` plugin in the same list, the filter should use the **exposed** (post-rename) tool names, since it sees the list after renaming.

### `notion_access`

Content-based access control for Notion MCP upstreams. Enforces per-bot, per-page read/write permissions using emoji markers embedded in the first line of each page. See [README_NOTION.md](README_NOTION.md) for full details.

```yaml
- type: notion_access
  bot_name: OcelliBot
```

### `inventory`

Writes a pretty-printed JSON file with the latest known inventory of tools, resources, and prompts. The file is rewritten each time a list hook fires, so it always reflects the most recent state.

| Field | Default | Description |
|---|---|---|
| `inventory_file` | required | Path to the JSON output file. Parent dirs are created automatically. |

**Snapshot format:**

```json
{
  "ts": "2026-03-07T12:00:00.000000+00:00",
  "tools": [
    {
      "name": "fetch",
      "description": "Fetches a URL from the internet.",
      "parameters": { "type": "object", "properties": { "url": { ... } } }
    }
  ],
  "resources": [
    { "uri": "file:///docs", "name": "docs", "description": "...", "mime_type": "text/plain" }
  ],
  "prompts": [
    { "name": "summarize", "description": "Summarize a document." }
  ]
}
```

Sections appear incrementally — `tools` is present after the first `tools/list`, `resources` after the first `resources/list`, etc.

## Claude Code Skills

This project includes two skills for [Claude Code](https://claude.com/claude-code) that automate MCP security analysis. The intended workflow is:

1. **Set up the proxy** — create a config pointing at your upstream MCP server(s) with logging and inventory plugins enabled.
2. **Run `/probe-mcp`** — Claude interactively probes the server: discovers tools, tests them with safe inputs, and (with your approval) tests for SSRF, path traversal, and other security issues. Produces a structured report.
3. **Run `/propose-filters`** — Claude analyzes the probe results and audit logs, then proposes mitigations. These range from simple YAML filter/rewrite config to custom Python plugins that inspect request arguments or response content (e.g. URL domain allowlists, PII redaction, metadata gates). Claude writes the plugin code, config models, server wiring, and tests.

### `/probe-mcp`

Systematically probes the MCP proxy to map its capabilities and security surface. Asks for explicit approval before running any potentially dangerous tests (SSRF vectors, file:// scheme, cloud metadata endpoints, etc.). Outputs a structured report with findings and recommendations.

### `/propose-filters`

Analyzes inventory, audit logs, and probe results to propose security mitigations at three levels:

- **Level 1 — YAML filter config**: block or allow tools/resources/prompts by glob pattern
- **Level 2 — YAML rewrite config**: lock down specific arguments to safe values
- **Level 3 — Custom plugins**: content-aware Python plugins that inspect request arguments or response bodies (e.g. restrict a `fetch` tool to approved domains, redact PII from responses, gate Notion reads by workspace ID)

Reviews each proposal with you before implementing. For custom plugins, follows the full project convention: config model, plugin class, server wiring, and tests.

## Proxy Features

The underlying proxy is based on FastMCP, and has all the expected features:

- **Multi-upstream aggregation** — proxy multiple MCP servers into a single endpoint with namespaced tools
- **Plugin pipeline** — stack logging, filtering, and rewriting plugins per upstream or globally
- **Filter plugin** — allow-list or deny-list tools, resources, and prompts by glob pattern
- **Rewrite plugin** — rename tools, inject fixed arguments, prefix response text
- **Logging plugin** — structured JSONL audit log of all operations with timing
- **Notion access plugin** — per-bot, per-page read/write access control using in-page permission markers
- **Inventory plugin** — JSON snapshot of all available tools, resources, and prompts for offline analysis
- **Stdio and HTTP transports** — upstream and proxy transports are independently configurable
- **OAuth 2.0 support** — HTTP upstreams can authenticate via OAuth with persistent token storage
- **Environment variable expansion** — `${VAR}` references in config values

## Examples

| File | Description |
|---|---|
| [examples/basic_proxy.yaml](examples/basic_proxy.yaml) | Transparent single-upstream proxy |
| [examples/multi_upstream.yaml](examples/multi_upstream.yaml) | Two upstreams with namespacing and a shared audit log |
| [examples/security_filter.yaml](examples/security_filter.yaml) | Full stack: logging + filtering + rewriting |

## Development

```bash
# Install dev dependencies
uv sync --group dev

# Run tests
uv run pytest tests/ -v

# Run a specific example
uv run mcp-proxy --config examples/basic_proxy.yaml
```

## CLI Reference

```
Usage: mcp-proxy [OPTIONS]

Options:
  -c, --config PATH                    Path to proxy YAML config file.  [required]
  --transport [stdio|http|streamable-http]
                                       Override the transport from the config file.
  --host TEXT                          Override the host for HTTP transport.
  --port INTEGER                       Override the port for HTTP transport.
  --help                               Show this message and exit.
```

## TODOs

- [ ] Some way for Claude to interactively filter and simplify logs.
- [ ] A fully worked example.
