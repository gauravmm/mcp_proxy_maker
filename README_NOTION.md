# Notion Access Control Plugin

The `notion_access` plugin enforces per-bot, per-page read/write permissions on a Notion MCP upstream. Permissions are declared using emoji markers on the first line of each Notion page's content, so workspace owners control access by editing pages directly in Notion.

## How it works

1. **Markers in page content.** The first line of a Notion page lists which bots have access and at what level. The format is `BotName EMOJI`, where the emoji indicates the access level:

   | Marker | Access level |
   |--------|-------------|
   | `OcelliBot 🖊` | Read-write (can read, edit, comment, duplicate, move) |
   | `OcelliBot 👀` | Read-only (can read and view comments) |

   Multiple bots can be listed comma-separated on the same line. If a bot appears with both emojis, the highest level (read-write) wins.

   Example first line:

   ```
   OcelliBot 🖊, ReviewBot 👀, AdminBot 🖊
   ```

2. **Transparent permission lookup.** The plugin checks its cache before every write operation:
   - When a bot calls `notion-fetch`, the plugin lets the request through to Notion, inspects the response for permission markers, caches the result, and either returns the page content or blocks with an access denied error.
   - For all write operations (`notion-update-page`, `notion-create-comment`, etc.), if the page's permission isn't cached yet, the proxy automatically fetches it in the background, checks for a marker, and then proceeds or blocks. The bot never needs to call `notion-fetch` explicitly before writing.

3. **Permission marker protection.** The first line containing the markers is treated as immutable:
   - `update_content` operations that target text matching the first line are blocked.
   - `replace_content` operations automatically prepend the cached first line, so it can't be accidentally removed.
   - `notion-create-pages` under a parent with cached permissions automatically inherits the parent's first line into each new child page's content.

## Configuration

```yaml
upstreams:
  - name: notion
    transport:
      type: http
      url: "https://mcp.notion.com/mcp"
      oauth: {}

    plugins:
      - type: notion_access
        bot_name: OcelliBot            # required: name to look for in markers
        read_emoji: "👀"               # default: eyes emoji
        write_emoji: "🖊"              # default: pen emoji (no variation selector)
        cache_ttl_seconds: 60          # default: 60; how long cached permissions last
        allow_workspace_creation: false # default: false; allow creating pages without a parent
        block_tools:                   # default: below; tools to hide and block entirely
          - notion-create-database
          - notion-update-data-source
```

### Config fields

| Field | Default | Description |
|-------|---------|-------------|
| `bot_name` | required | The bot name to search for in permission markers. |
| `read_emoji` | `👀` | Emoji that grants read-only access. |
| `write_emoji` | `🖊` | Emoji that grants read-write access. |
| `cache_ttl_seconds` | `60` | Seconds before a cached permission expires. After expiry, the page must be fetched again. |
| `allow_workspace_creation` | `false` | Whether `notion-create-pages` without a `parent.page_id` is allowed. When false, all new pages must be created under an existing parent. |
| `block_tools` | `["notion-create-database", "notion-update-data-source"]` | Tools to block entirely. These are hidden from tool listings and rejected at call time. |

## Per-tool behavior

| Tool | Required access | Notes |
|------|----------------|-------|
| `notion-search` | None | Always allowed (workspace-level search). |
| `notion-get-teams` | None | Always allowed. |
| `notion-get-users` | None | Always allowed. |
| `notion-fetch` | None (checked on response) | Request passes through; response is inspected for markers. If no marker is found for the bot, the response is replaced with an access denied error. Permission is cached for subsequent operations. Write tools also trigger an automatic background fetch when the page is not yet cached. |
| `notion-get-comments` | READ | Page must have been fetched first. |
| `notion-update-page` | WRITE | Includes first-line protection (see below). |
| `notion-create-comment` | WRITE | Page must have been fetched with write access. |
| `notion-duplicate-page` | WRITE | Page must have been fetched with write access. |
| `notion-move-pages` | WRITE on all pages | Every page in `page_or_database_ids` must have cached write access. |
| `notion-create-pages` | WRITE on parent | Parent's first line is inherited into each new child page (any LLM-provided marker line is stripped first). If no parent is specified, controlled by `allow_workspace_creation`. |
| `notion-create-database` | Blocked | Blocked by default via `block_tools`. |
| `notion-update-data-source` | Blocked | Blocked by default via `block_tools`. |

## First-line protection

The permission marker line is protected from modification:

- **`update_content`**: If any `old_str` in `content_updates` matches text found in the cached first line, the operation is blocked. Body edits that don't touch the first line pass through normally.
- **`replace_content`**: The cached first line is automatically prepended to `new_str`, so the markers survive a full content replacement.
- **`create-pages` with parent**: When a top-level `parent` with a `page_id` is provided, each new child page's `content` field is prepended with the parent's first line, inheriting the same access markers. If the LLM already included a marker line (any line containing the read or write emoji), it is stripped first to prevent duplication. The proxy is always the authority on the marker — the parent's exact first line is used regardless of what the caller provides.

## Cache behavior

Permissions are cached in memory keyed by page ID, with a configurable TTL (default 60 seconds). When a cached entry expires, the next write operation triggers a fresh background fetch automatically. This means:

- Changes to permission markers in Notion take effect within `cache_ttl_seconds`.
- There is no persistence across proxy restarts; permissions are re-fetched on first access after restart.

## Uploading images (`notion-upload-image`)

The plugin registers a synthetic `notion-upload-image` tool when `notion_token` is set in the config. This tool uploads a local image file to Notion and replaces a placeholder text block with a proper image block.

### Why a placeholder?

The Notion MCP server has no native file-upload tool. The two-step placeholder approach works around this: you first insert a sentinel text block (using `notion-update-page`), then call `notion-upload-image`, which finds that block, uploads the file directly to the Notion API, removes the placeholder, and inserts an image block in its place.

### Configuring the plugin extension

Add `notion_token` to the plugin config. This must be a Notion internal integration token with access to the target workspace — it is used for direct Notion API calls that the MCP server cannot handle.

```yaml
plugins:
  - type: notion_access
    bot_name: OcelliBot
    notion_token: "${NOTION_TOKEN}"   # internal integration bearer token
    # ... other fields
```

### Usage

1. **Insert the placeholder** using `notion-update-page`. The placeholder format is `[IMAGE_UPLOAD: /absolute/path/to/file.png]`:

   ```
   notion-update-page  →  command: update_content
                          content_updates: [{old_str: "...", new_str: "...\n[IMAGE_UPLOAD: /home/user/poster.png]"}]
   ```

2. **Upload the image** (permission is checked and auto-fetched if needed):

   ```
   notion-upload-image  →  page_id: <your-page-id>
                           file_path: /home/user/poster.png
                           caption: "Optional caption"   # optional
   ```

   The tool will:
   - Locate the `[IMAGE_UPLOAD: ...]` paragraph block on the page
   - Create a Notion file upload session and upload the file
   - Delete the placeholder block
   - Append an image block at the same position

### Tool parameters

| Field | Required | Description |
|-------|----------|-------------|
| `page_id` | Yes | ID of the Notion page (with or without dashes). |
| `file_path` | Yes | Absolute path to the local image file. |
| `caption` | No | Caption text for the image block. |

The tool requires WRITE access on the target page (confirmed via the fetch-and-cache mechanism). If the placeholder is not found on the page, the call fails with an error — insert the placeholder with `notion-update-page` before calling this tool.

## Example workflow

A typical interaction looks like this:

1. Bot calls `notion-update-page` on a page it has never fetched.
2. Plugin finds no cache entry, automatically fetches the page in the background, finds `OcelliBot 🖊` on the first line, caches WRITE access, and allows the update.
3. Bot calls `notion-create-pages` under the same page. Plugin checks (from cache) that the parent has WRITE access, prepends the parent's first line to the new page's content.
4. Bot calls `notion-fetch` on a page that has no markers. Plugin replaces the response with `[ACCESS DENIED] No permission marker for OcelliBot on this page.`
5. Bot attempts to write to that page. Plugin auto-fetches again (cache miss or TTL expired), finds no marker, and blocks the write.

## OAuth token refresh workaround

The proxy includes a workaround for a fastmcp bug that causes spurious browser OAuth re-authorization flows after a proxy restart.

### The problem

fastmcp's `OAuth._initialize` loads stored tokens from disk and recomputes the expiry as `now + expires_in`. This makes a stale access token (e.g. issued hours ago with a 1-hour TTL) appear valid for another full hour. When the token is sent to Notion and rejected with a 401, the auth flow jumps directly to a full browser-based OAuth flow **without first trying the stored refresh token**.

The result: the browser OAuth window re-opens intermittently after proxy restarts, depending on how long the access token has actually been expired.

### The fix

`_RefreshOnStartOAuth` (in `server.py`) subclasses `OAuth` and marks loaded tokens as already expired after initialization. This forces the first request to take the refresh-token path instead of blindly trusting the stale access token. The browser flow only opens if the refresh token itself has been revoked or expired.

This workaround can be removed once fastmcp fixes the upstream bug (either by storing the actual expiry timestamp or by attempting a refresh on 401 before falling back to full re-authorization).
