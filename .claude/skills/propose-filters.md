# Propose Security Filters

Analyze MCP proxy logs and inventory, then propose and implement security mitigations — from simple YAML config to custom content-aware plugins.

## Instructions

You are a security analyst for an MCP proxy. Your job is to examine probe results and logs, then propose and build appropriate mitigations. Mitigations range from simple YAML filter config to custom Python plugins that inspect request arguments and response content.

### 1. Gather Data

- Read `logs/inventory.json` for the tool/resource/prompt inventory (names, descriptions, parameter schemas).
- Read `logs/audit.jsonl` for all recorded interactions (arguments passed, responses received, errors).
- If neither file exists, use AskUserQuestion to ask the user to run `/probe-mcp` first or to point you at relevant log/inventory files.

### 2. Analyze Threats

From the inventory, classify each tool by risk:

**Argument-based risks** (what the caller can send):

- Tools accepting URLs → SSRF (internal networks, cloud metadata, file:// scheme)
- Tools accepting file paths → path traversal, access to sensitive files
- Tools accepting commands/code → injection, arbitrary execution
- Tools accepting IDs/names → unauthorized access to other users' data

**Response-based risks** (what comes back):

- Tools returning page content → data exfiltration, prompt injection via fetched content
- Tools returning file contents → credential/secret leakage
- Tools returning structured data → metadata exposure (user IDs, emails, internal URLs)

**Behavioral risks** (how the tool behaves):

- Tools with side effects (write, delete, send) → destructive or irreversible actions
- Tools with no rate limiting → resource exhaustion
- Tools that can reach internal services → lateral movement

From the audit log, look for:

- Actual dangerous arguments that were passed (internal IPs, file:// URLs, sensitive paths)
- Responses containing credentials, tokens, internal hostnames, or PII
- Error patterns suggesting the tool attempted something it shouldn't

### 3. Ask the User About Context

Use AskUserQuestion to understand:

- What is this proxy used for? (e.g. "web research agent", "Notion assistant", "project management bot")
- Which tools must remain accessible?
- What data sensitivity level? (public, internal, confidential)
- Should mitigations be strict (default-deny) or targeted (block known-bad)?

### 4. Propose Mitigations

For each identified risk, propose the appropriate level of mitigation. Present these to the user grouped by category.

#### Level 1: YAML filter config (for simple allow/block by name)

Use when tools can be entirely allowed or blocked by name:

```yaml
- type: filter
  block_tools: ["delete_*", "write_*"]    # Block all destructive tools
  allow_resources: ["notion://pages/*"]    # Only allow specific resource patterns
```

#### Level 2: YAML rewrite config (for locking down arguments)

Use when a tool is needed but specific arguments should be constrained:

```yaml
- type: rewrite
  argument_overrides:
    fetch:
      max_length: 10000    # Prevent unbounded responses
```

#### Level 3: Custom content-aware plugin (for inspecting arguments or responses)

Use when you need to examine the *content* of requests or responses — not just names. **Write a real plugin** that subclasses `PluginBase`.

Examples of when custom plugins are needed:

- **URL allowlist plugin**: A `fetch` tool should only access specific domains, not just any URL. Inspect `params.arguments["url"]` and check against an allowlist of domains.
- **Metadata gate plugin**: A Notion MCP exposes pages, but reads should only be allowed for pages in certain workspaces. Inspect resource URIs or response content for workspace IDs.
- **PII redaction plugin**: Tool responses may contain emails, API keys, or internal URLs. Inspect `result.content` text blocks and redact patterns before returning.
- **Argument sanitization plugin**: A project management tool accepts queries — ensure the query doesn't contain SQL injection or other payloads.
- **Response size limiter plugin**: Cap the total text length of responses to prevent context flooding.
- **Side-effect confirmation plugin**: For tools that write or delete, log the full request and inject a warning prefix into the response.

When writing a custom plugin:

1. Create the plugin file in `mcp_proxy/plugins/` following the pattern in `filter_plugin.py` and `rewrite_plugin.py`.
2. Subclass `PluginBase` from `mcp_proxy.plugins.base`.
3. Override only the hooks you need. Available hooks:
   - `on_call_tool_request(params)` → inspect/modify/block tool call arguments
   - `on_call_tool_response(params, result)` → inspect/modify/redact tool responses
   - `on_list_tools(tools)` → hide tools from listing
   - `on_read_resource_request(params)` → inspect/block resource reads
   - `on_read_resource_response(params, result)` → inspect/redact resource content
   - `on_list_resources(resources)` → hide resources from listing
   - `on_get_prompt_request(params)` / `on_get_prompt_response(params, result)` / `on_list_prompts(prompts)`
4. To block a request, raise `McpError(ErrorData(code=-32601, message="..."))`.
5. Add a config model in `config/schema.py` with `type: Literal["yourtype"]` and add it to the `PluginConfig` union.
6. Add a branch in `server.py:_build_plugin()` to instantiate it.
7. Add tests in `tests/test_plugins.py`.

### 5. Review and Implement

Use AskUserQuestion to present the full proposal:

- List each mitigation with its rationale
- For custom plugins, describe what the plugin will do before writing code
- Ask which mitigations to implement

Then implement the approved mitigations:

- For Level 1/2: write a complete example YAML config to `examples/`
- For Level 3: write the plugin code, config model, server wiring, and tests — following the project's "Adding a New Plugin" pattern from CLAUDE.md

After implementation, use AskUserQuestion to ask if the user wants to run the tests to verify.
