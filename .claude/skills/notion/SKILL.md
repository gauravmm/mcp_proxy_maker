---
name: notion
description: Work with Notion pages via the access-controlled MCP proxy. Covers reading, writing, creating pages, and uploading images.
---

# Use Notion via MCP Proxy

Work with Notion pages through the access-controlled MCP proxy. Always follow the fetch-then-act pattern: fetch a page before any write operation.

## Instructions

### Reading pages

Fetch a page by URL or ID:

```
notion-fetch  →  id: "https://www.notion.so/My-Page-abc123"
```

The proxy checks the page's first line for a permission marker (`BotName 🖊` for read-write, `BotName 👀` for read-only). If your bot is not listed, the response is replaced with an access denied error. On success, the permission is cached for subsequent operations.

Search across the workspace without needing to fetch first:

```
notion-search  →  query: "..."
```

### Writing pages

Always fetch the page first to establish write permission in the cache, then update.

**Edit specific content** (preferred — surgical, safe):

```
notion-update-page  →  page_id: "..."
                        command: update_content
                        content_updates: [
                          { old_str: "old text", new_str: "new text" }
                        ]
```

**Replace entire content** (the proxy automatically re-prepends the permission marker line):

```
notion-update-page  →  page_id: "..."
                        command: replace_content
                        new_str: "# New content\n..."
```

**Do not edit the first line** of any page — it contains the permission markers and is protected. Any `update_content` operation targeting text from the first line will be blocked.

### Creating pages

New pages under a parent automatically inherit the parent's permission markers. Fetch the parent first:

```
notion-fetch      →  id: "<parent-page-id>"
notion-create-pages  →  parent: { page_id: "<parent-page-id>" }
                         ...
```

### Uploading images

The Notion MCP server has no native file-upload capability. Use the two-step placeholder workflow:

**Step 1** — Fetch the target page (establishes write permission), then insert a placeholder using `notion-update-page`:

```
notion-update-page  →  page_id: "..."
                        command: update_content
                        content_updates: [
                          {
                            old_str: "line before image",
                            new_str: "line before image\n[IMAGE_UPLOAD: /absolute/path/to/image.png]"
                          }
                        ]
```

**Step 2** — Upload the image (finds the placeholder, uploads the file, replaces it with an image block):

```
notion_upload_image  →  page_id: "..."
                         file_path: "/absolute/path/to/image.png"
                         caption: "Optional caption"
```

The tool handles everything: creates a Notion file upload session, sends the bytes, deletes the placeholder, and appends the image block at the same position.

**Important:** the placeholder text must exactly match `[IMAGE_UPLOAD: /path/to/file]` (same path as `file_path`). If the placeholder is missing, the upload fails with an error — re-insert it and retry.

### Notion-flavored Markdown primer

Standard Markdown mostly works. Key differences:

- **Indentation** uses tabs, not spaces, for nested children under list items, callouts, etc.
- **Blank lines** are stripped — use `<empty-block/>` on its own line instead.
- **Headings 5–6** are converted to h4.
- **Escape chars** extend to: `$ [ ] < > { } | ^` (in addition to the usual `\ \* ~ \``)
- **Multi-line quotes** use `<br>` inside a single `>` block — multiple `>` lines render as separate blocks, not one.
- **Tables** use HTML syntax, not pipe syntax (`| col |`):

  ```
  <table header-row="true">
  	<tr><td>**A**</td><td>**B**</td></tr>
  	<tr><td>cell</td><td>cell</td></tr>
  </table>
  ```

- **Colors** — append `{color="blue_bg"}` to a block's first line, or use `<span color="red">text</span>` inline. Colors: `gray brown orange yellow green blue purple pink red` and `*_bg` variants.
- **Underline** — `<span underline="true">text</span>` (no Markdown equivalent).
- **Callout** — `<callout icon="💡">children</callout>` (tab-indented).
- **Toggle** — `<details><summary>Title</summary>children</details>` (tab-indented).
- **Columns** — `<columns><column>...</column><column>...</column></columns>`.
- **Page vs mention** — `<page url="...">` embeds a child page and _moves_ an existing page into this one. Use `<mention-page url="...">` to link without moving.

#### Common errors

| Error                                              | Cause                                         | Fix                                                           |
| -------------------------------------------------- | --------------------------------------------- | ------------------------------------------------------------- |
| `[ACCESS DENIED] No permission marker`             | Bot not listed on this page                   | Ask the workspace owner to add `BotName 🖊` to the first line |
| `[ACCESS DENIED] Page must be fetched first`       | Write attempted without a prior fetch         | Call `notion-fetch` on the page first                         |
| `[ACCESS DENIED] Cannot modify permission markers` | Edit targeted the first line                  | Adjust `old_str` to not include the first line                |
| `Placeholder '...' not found`                      | Placeholder was not inserted or path mismatch | Insert the placeholder with `notion-update-page` first        |
