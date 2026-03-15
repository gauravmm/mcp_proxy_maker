# Hive Access Control Plugin

The `hive_access` plugin enforces workspace and project-level scoping on a [Hive](https://app.hive.com) MCP upstream. It ensures the agent can only read and modify actions within an explicitly configured set of projects, and prevents data leakage across workspace boundaries.

## How it works

1. **Workspace injection.** Every tool that accepts a `workspaceId` parameter has it automatically injected from the config. The agent cannot reach a different workspace by omitting or overriding this field.

2. **Project allowlist enforcement.** A set of `allowed_project_ids` is declared in the config. The plugin enforces this allowlist on every tool that scopes by project:
   - `getActions` — the agent may pass a subset of the allowlist, but never projects outside it. If no `projectIds` are specified, the full allowlist is injected automatically. Passing an explicit `null` is blocked.
   - `getProjects` — similarly clamped to the allowlist via `specificIds`. `includePrivate: true` is blocked.
   - `insertActions` — every action object must include a `projectId` from the allowlist.

3. **Write-tool action verification.** Tools that modify actions by `actionIds` (not `projectId`) use a two-level lookup to verify ownership:
   - **Cache (fast path).** `getActions` responses are inspected and each action's `projectId` is stored in a session-lifetime in-memory cache. Since an action's project is immutable, this cache never needs invalidation.
   - **Fetch (slow path).** If an `actionId` isn't cached yet, the plugin calls `getActions(specificIds=[...])` on the upstream to populate the cache before checking.
   - If any action belongs to a project outside the allowlist, the call is blocked.

## Configuration

```yaml
upstreams:
  - name: hive
    transport:
      type: http
      url: "https://..."          # your Hive MCP endpoint
      persistent_connection: true  # required for write-tool verification
    plugins:
      # filter runs first: blocks ~67 high-risk tools, leaving only the ~15 below.
      # hive_access then scopes those 15 to the configured workspace and projects.
      - type: filter
        allow_tools:
          - getWorkspace
          - getProjects
          - getActions
          - actionComments
          - getLabels
          - getPriorityLevels
          - groups
          - insertActions
          - updateActionsStatus
          - updateActionsTitles
          - updateActionsDescription
          - updateActionsAssignees
          - updateActionsLabels
          - updateActionsMilestone
          - updateActionsPriorityLevelId
      - type: hive_access
        workspace_id: "EXAMPLE_WORKSPACE_ID"
        allowed_project_ids:
          - "EXAMPLE_PROJECT_ID_1"
          - "EXAMPLE_PROJECT_ID_2"
```

**Plugin order matters.** `filter` must come before `hive_access` in the plugins list. The filter blocks ~67 high-risk tools first; `hive_access` then enforces scope on the remaining allowed tools.

### Config fields

| Field | Default | Description |
|-------|---------|-------------|
| `workspace_id` | required | The Hive workspace ID. Injected into every tool that accepts `workspaceId`. |
| `allowed_project_ids` | required | List of project IDs the agent is permitted to access. Must be non-empty. |
| `hide_blocked` | `true` | When `true`, blocked items are hidden from `tools/list` responses. |

### `persistent_connection`

Set `persistent_connection: true` on the HTTP transport. This causes the proxy to maintain a long-lived upstream client, which the plugin reuses to make verification calls (`getActions(specificIds=[...])`) when an action isn't in the cache yet. Without it, write-tool verification falls back to a warning-and-allow behavior.

## Per-tool behavior

| Tool | Enforcement |
|------|-------------|
| `getWorkspace` | Injects `workspaceId` if absent. |
| `getProjects` | Clamps `specificIds` to allowlist (narrowing allowed, widening blocked). Blocks `includePrivate: true`. |
| `getActions` | Clamps `projectIds` to allowlist. Injects full allowlist if absent. Blocks explicit `null`. |
| `actionComments` | Passed through (scoping by action ID, not project). |
| `getLabels`, `getPriorityLevels`, `groups` | Passed through. |
| `getNotebooks` | Injects `workspaceId` if absent. |
| `insertActions` | Each action object must have a `projectId` from the allowlist. |
| `updateActionsStatus` | Verifies all `actionIds` belong to allowed projects (cache + upstream fetch). |
| `updateActionsTitles` | Same as above. |
| `updateActionsDescription` | Same as above. |
| `updateActionsAssignees` | Same as above. |
| `updateActionsLabels` | Same as above. |
| `updateActionsMilestone` | Same as above. |
| `updateActionsPriorityLevelId` | Same as above. |

## Cache behavior

The action→project cache is an in-memory `dict[str, str]` with no TTL. It is populated from every `getActions` response and persists for the life of the proxy session. Since actions cannot change their parent project, the cache is always valid.

On a proxy restart, the cache is empty. The first `getActions` call will repopulate it for any returned actions. Write tools called before a covering `getActions` call will trigger an upstream fetch to verify ownership; if the upstream call fails, the write is blocked conservatively.

## Security design

The recommended deployment pairs `filter` + `hive_access`:

| Layer | What it does |
|-------|-------------|
| `filter` | Blocks ~67 tools outright (archive, salesforce, duplicate, news posts, messages, emails, etc.) |
| `hive_access` | Scopes the ~15 remaining tools to a single workspace and an explicit project allowlist |

Notable tools blocked by the recommended `filter` config:

- `insertNewsPost` — broadcasts to entire workspace
- `insertMessage` / `getMessages` — free-form messaging and search across all groups
- `getEmails` — reads connected email, no workspace scope
- `salesforceOperation` — full external CRM CRUD
- `archiveActions`, `duplicateAction`, `convertActionToProject` — destructive or scope-escaping operations
- All approval, dashboard, resource assignment, and goal tools
