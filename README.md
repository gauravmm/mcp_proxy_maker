# MCP Security Proxy

A configurable proxy for [Model Context Protocol (MCP)](https://modelcontextprotocol.io) servers. Sits between an MCP client (e.g. Claude Desktop) and one or more upstream MCP servers, applying a plugin pipeline for logging, filtering, and rewriting requests and responses.

## Features

- **Multi-upstream aggregation** — proxy multiple MCP servers into a single endpoint with namespaced tools
- **Plugin pipeline** — stack logging, filtering, and rewriting plugins per upstream or globally
- **Filter plugin** — allow-list or deny-list tools, resources, and prompts by glob pattern
- **Rewrite plugin** — rename tools, inject fixed arguments, prefix response text
- **Logging plugin** — structured JSONL audit log of all operations with timing
- **Inventory plugin** — JSON snapshot of all available tools, resources, and prompts for offline analysis
- **Stdio and HTTP transports** — upstream and proxy transports are independently configurable
- **Environment variable expansion** — `${VAR}` references in config values

## Installation

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <repo>
cd mcp-security-proxy-maker
uv sync
```

## Quick Start

Create a config file (see [examples/](examples/)) and run:

```bash
uv run mcp-proxy --config examples/basic_proxy.yaml
```

### Claude Desktop integration

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or the equivalent on your platform:

```json
{
  "mcpServers": {
    "proxy": {
      "command": "uv",
      "args": [
        "run",
        "--project", "/path/to/mcp-security-proxy-maker",
        "mcp-proxy",
        "--config", "/path/to/proxy.yaml"
      ]
    }
  }
}
```

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
```

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

**Log entry fields:**

| Field | Description |
|---|---|
| `schema_version` | Always `1` |
| `ts` | ISO 8601 UTC timestamp |
| `event` | `request`, `response`, `list` |
| `method` | MCP method (`tools/call`, `resources/read`, etc.) |
| `tool_name` | Tool name (tool operations only) |
| `resource_uri` | Resource URI (resource operations only) |
| `prompt_name` | Prompt name (prompt operations only) |
| `arguments` | Request arguments (null if `include_payloads: false`) |
| `is_error` | Whether the response was an error (response only) |
| `content_blocks` | Number of content blocks in response (response only) |
| `content_length_chars` | Total text length of response (response only) |
| `duration_ms` | Round-trip time in milliseconds (response only) |
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
