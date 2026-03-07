# Probe MCP

Systematically probe the MCP proxy to understand its tools, resources, prompts, and security posture. Makes requests through the proxy, asking the user before attempting anything dangerous.

## Instructions

You are probing an MCP proxy to build a comprehensive understanding of its capabilities and security surface. Follow these steps:

### 1. Inventory Discovery

- Read `logs/inventory.json` if it exists to get the current snapshot of tools, resources, and prompts.
- Read `logs/audit.jsonl` if it exists to review past interactions.
- Summarize what you find: tool names, descriptions, parameter schemas, resources, and prompts.
- If no inventory exists, try calling a benign tool to trigger list hooks, then re-read the inventory.

### 2. Confirm Scope with the User

Use AskUserQuestion to:

- Show the discovered tool list with brief descriptions
- Ask which MCP server connection to probe (if multiple are available via different namespaces)
- Ask if they want a full probe (safe + unsafe) or just safe probing
- Ask if there are any tools or endpoints they want to skip

### 3. Safe Probing

For each tool discovered, test it with benign inputs to understand its behavior. For each test, record:

- The tool name and arguments sent
- Whether it succeeded or errored
- The response format and content type
- Any interesting metadata in the response

**Examples of safe probes by tool type:**

For URL/fetch tools:

- Simple HTTP: `http://example.com`
- HTTPS: `https://httpbin.org/get` (also reveals outgoing IP and headers)
- Invalid URL to test error handling
- Missing required params to test validation

For file/filesystem tools:

- Read a known-safe path
- List a known-safe directory
- Read a nonexistent path to test error handling

For search/query tools:

- Simple query with expected results
- Empty query
- Very long query to test limits

For any tool:

- Call with no arguments (test required param validation)
- Call with extra unexpected arguments (test strict vs. permissive parsing)

### 4. Potentially Unsafe Probing

**Before running ANY of these, use AskUserQuestion to get explicit approval.** Group probes by category, explain what each tests, and let the user pick which categories to run.

**Category A — SSRF / Network boundary testing:**
Tests whether the tool can reach internal services.

- `http://127.0.0.1` / `http://localhost` / `http://[::1]` — loopback
- `http://0.0.0.0` — wildcard bind address
- `http://169.254.169.254/latest/meta-data/` — AWS instance metadata
- `http://metadata.google.internal/` — GCP metadata
- `http://10.0.0.1` / `http://192.168.1.1` — private networks

**Category B — Scheme and protocol testing:**
Tests whether non-HTTP schemes are accepted.

- `file:///etc/passwd` — local file read
- `file:///etc/hostname` — hostname disclosure
- `ftp://example.com` — FTP scheme
- `data:text/html,<h1>test</h1>` — data URI

**Category C — Response boundary testing:**
Tests resource limits and edge cases.

- Very large response: `https://httpbin.org/bytes/1000000`
- Slow response: `https://httpbin.org/delay/10`
- Redirect chain: `https://httpbin.org/redirect/5`
- Binary content: `https://httpbin.org/image/png`

**Category D — Content injection testing:**
Tests whether response content could be used for prompt injection.

- Fetch a page known to contain instructions (e.g. a paste with "ignore previous instructions")
- Fetch a page that returns unusual content types

**Category E — Path traversal (for file tools):**

- `../../../etc/passwd`
- Absolute paths outside expected directories
- Symlink targets

### 5. Analyze and Report

After probing, produce a structured report. Save the findings so `/propose-filters` can use them.

```markdown
## MCP Probe Report

### Environment
- Proxy config: [which example YAML was used]
- Upstreams probed: [list of upstream names/namespaces]
- Date: [timestamp]

### Tool Inventory
For each tool:
- **[name]**: [description]
  - Parameters: [list with types and whether required]
  - Observed behavior: [what it does, response format]

### Resource Inventory
- [uri]: [name], [mime_type]

### Prompt Inventory
- [name]: [description]

### Security Findings

#### Input Validation
- [ ] URL scheme restriction: [does it block file://, ftp://, etc.?]
- [ ] Loopback/private IP blocking: [does it block 127.0.0.1, 10.x, etc.?]
- [ ] Path traversal protection: [does it block ../..?]
- [ ] Argument type validation: [does it reject wrong types?]

#### Network Boundary
- [ ] Can reach loopback: [yes/no, which addresses]
- [ ] Can reach cloud metadata: [yes/no]
- [ ] Can reach private networks: [yes/no]

#### Response Handling
- [ ] Response size limits: [observed max]
- [ ] Timeout behavior: [what happens with slow responses]
- [ ] Binary content handling: [returned or rejected]

#### Content Risks
- [ ] Responses could contain prompt injection: [yes/no, examples]
- [ ] Responses could contain credentials/PII: [yes/no, examples]

### Recommendations
Prioritized list of mitigations, from most critical to nice-to-have.
Each recommendation should note whether it needs:
- YAML config (filter/rewrite plugin)
- Custom plugin (content-aware inspection)
```

Use AskUserQuestion to ask if the user wants to:

1. Save this report to a file (suggest `logs/probe_report.md`)
2. Immediately run `/propose-filters` to generate mitigations
