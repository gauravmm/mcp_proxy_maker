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

2. **Fetch-then-act pattern.** The plugin uses a cache-based approach:
   - When a bot calls `notion-fetch`, the plugin lets the request through to Notion, inspects the response for permission markers, caches the result, and either returns the page content or blocks with an access denied error.
   - All write operations (`notion-update-page`, `notion-create-comment`, etc.) check the cache. If the page hasn't been fetched yet, the request is blocked with an error asking the caller to fetch the page first.

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
| `notion-fetch` | None (checked on response) | Request passes through; response is inspected for markers. If no marker is found for the bot, the response is replaced with an access denied error. Permission is cached for subsequent operations. |
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
- **`create-pages` with parent**: Each new child page's `content` field is prepended with the parent's first line, inheriting the same access markers. If the LLM already included a marker line (any line containing the read or write emoji), it is stripped first to prevent duplication. The proxy is always the authority on the marker — the parent's exact first line is used regardless of what the caller provides.

## Cache behavior

Permissions are cached in memory keyed by page ID, with a configurable TTL (default 60 seconds). When a cached entry expires, the page must be fetched again before any operations are allowed. This means:

- A bot must always `notion-fetch` a page before writing to it.
- Changes to permission markers in Notion take effect within `cache_ttl_seconds`.
- There is no persistence across proxy restarts; all pages must be re-fetched.

## Example workflow

A typical interaction looks like this:

1. Bot calls `notion-fetch` with a page ID.
2. Plugin inspects the response, finds `OcelliBot 🖊` on the first line, caches WRITE access.
3. Bot calls `notion-update-page` with `update_content` to edit the page body. Plugin checks cache, confirms WRITE access, verifies the edit doesn't target the first line, and allows it.
4. Bot calls `notion-create-pages` under the same page. Plugin checks parent has WRITE access, prepends the parent's first line to the new page's content.
5. Bot calls `notion-fetch` on a different page that has no markers. Plugin replaces the response with `[ACCESS DENIED] No permission marker for OcelliBot on this page.`
