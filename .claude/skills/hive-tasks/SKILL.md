# Hive Task Management

Manage tasks in the Hive workspace through the proxy: find, create, update, and schedule actions across allowed projects.

## Proxy Constraints

You are operating through a security proxy. Only the following tools are accessible:

| Tool | What it does |
|------|-------------|
| `getWorkspace` | Fetch workspace metadata |
| `getProjects` | List allowed projects (allowlist is enforced — you cannot widen it) |
| `getActions` | Search and retrieve actions (scoped to allowed projects) |
| `actionComments` | Read comments on an action |
| `getLabels` | List available labels |
| `getPriorityLevels` | List priority levels |
| `groups` | List messaging groups (members/teams) |
| `insertActions` | Create one or more new actions |
| `updateActionsStatus` | Change status of actions |
| `updateActionsTitles` | Rename actions |
| `updateActionsDescription` | Set action description (HTML) |
| `updateActionsAssignees` | Set or change assignees |
| `updateActionsLabels` | Apply or change labels |
| `updateActionsMilestone` | Mark/unmark actions as milestones |
| `updateActionsPriorityLevelId` | Set priority |

**Important limitations imposed by the proxy:**

- `getActions` without `projectIds` automatically scopes to allowed projects — do not pass `null`.
- `getProjects` without `specificIds` returns only allowed projects — this is correct behaviour.
- You cannot update deadlines or scheduled dates on existing actions through the current proxy config. Set them at creation time via `insertActions`.
- All blocked tools (emails, messages, news posts, etc.) will return a policy error — do not retry them.

---

## Instructions

### 1. Orient Yourself

Call `getWorkspace` (the `workspaceId` will be injected automatically by the proxy, so you may omit it or use the correct ID). Note the workspace name.

Call `getProjects` to discover which projects are accessible. Record the `_id` and `name` of each project — you will need these IDs for filtering.

Call `getPriorityLevels` and `getLabels` to build a reference map of IDs to names. These are needed when creating or updating actions.

### 2. Understand the User's Request

Use AskUserQuestion to clarify:

- **Find vs. create**: Are they looking for an existing action or creating a new one?
- **Project scope**: Which project(s) should be searched or used? Show the project list from Step 1.
- **Who**: Is this for a specific person (assignee), or unassigned?
- **When**: Is there a deadline or scheduled start date?

For vague requests like "add a TODO for X", assume: no assignee, no deadline, status = default (usually "To do"), and ask which project to use if there is more than one allowed project.

### 3. Finding Existing Actions

Use `getActions` to search. Key parameters:

```
text          — keyword search across title/description
projectIds    — filter to specific project(s); omit to search all allowed projects
assignees     — list of user IDs
status        — exact status string, e.g. "To do", "In Progress", "Completed"
specificIds   — look up known action IDs directly
excludeCompletedActions — true by default; pass false to include completed
first         — max 30 per page; use `after` cursor to paginate
```

**Search strategy:**

- Start broad (text search across all projects), then narrow.
- If the user provides a partial title, use `text` — do not guess an action ID.
- If the result set is large, summarise by project and status rather than listing every action. Ask the user if they want to narrow further.
- For "my actions" or "what's on my plate" queries, use `assignees` with the relevant user ID. Check `getWorkspace` members to find user IDs if needed.

### 4. Creating New Actions

Use `insertActions`. Each action object requires:

```json
{
  "workspace": "<workspaceId>",
  "title": "...",
  "projectId": "<allowed project ID>"
}
```

Optional fields commonly used for task management:

```json
{
  "description": "<HTML string>",
  "assignees": ["<userId>"],
  "deadline": "<ISO 8601 datetime>",
  "scheduledDate": "<ISO 8601 datetime>",
  "labels": ["<labelId>"],
  "priorityLevelId": "<priorityId>",
  "status": "To do",
  "urgent": false,
  "milestone": false,
  "parent": "<parentActionId>"
}
```

**Deadline notes:**

- `deadline` = when the action must be finished.
- `scheduledDate` = when work should begin.
- Both accept ISO 8601 datetime strings, e.g. `"2026-04-01T09:00:00.000Z"`.
- Deadlines **cannot be changed** on existing actions through the current proxy config — get clarification before creating if the date matters.
- If the user says "by end of day Friday", interpret relative to today's date (available in your system context as `currentDate`).

**Multiple related actions:** You can pass multiple objects in a single `insertActions` call. Use this for checklists or related sub-tasks. Set `parent` to the parent action's `_id` to create sub-actions.

### 5. Updating Existing Actions

First identify the target action(s) with `getActions`. Extract the `_id` field(s).

Then call the appropriate update tool with `actionIds: ["<id>", ...]`:

| Goal | Tool | Key param |
|------|------|-----------|
| Change title | `updateActionsTitles` | `titles: [{actionId, title}]` |
| Change status | `updateActionsStatus` | `status: "In Progress"` etc. |
| Add/change description | `updateActionsDescription` | `description: "<HTML>"` |
| Assign to someone | `updateActionsAssignees` | `assignees: ["<userId>"]` |
| Apply labels | `updateActionsLabels` | `labels: ["<labelId>"]` |
| Mark as milestone | `updateActionsMilestone` | `milestone: true` |
| Set priority | `updateActionsPriorityLevelId` | `priorityLevelId: "<id>"` |

Before calling any update tool, state what you are about to change and confirm with the user unless they have already been explicit (e.g. "mark action X as done" is unambiguous; "update the task" is not).

**Status values** in Hive are workspace-defined. Common defaults: `"To do"`, `"In Progress"`, `"Completed"`. Use the `status` field from a `getActions` response to see what statuses are in use.

### 6. Marking Tasks Complete

Marking an action done = `updateActionsStatus` with the workspace's completion status. Check a completed action's `customStatus` via `getActions` (pass `excludeCompletedActions: false` to retrieve them) to confirm the exact status string to use.

### 7. Scheduling and Deadlines

The proxy currently supports setting deadlines and scheduled dates **only at creation time**. If a user asks to change a deadline on an existing action, tell them this is not currently supported through the proxy and offer to:

1. Note the change as a comment (if `actionComments` returns a writable tool — currently it is read-only).
2. Update the action's description to record the new intended deadline.
3. Suggest they update it directly in the Hive app.

When helping plan work across multiple actions:

- Use `getActions` with `startDate`/`endDate` to find actions in a date window.
- Summarise in a table: title, project, status, deadline, assignee.
- Flag any `isBlocked: true` actions — they cannot proceed until their dependency is resolved.

### 8. Output Format

Unless the user asks for a specific format, respond with:

- **For searches**: a concise table (title, status, deadline, assignee, project). Omit empty columns.
- **For creates**: confirm what was created (title, project, deadline if set, ID for reference).
- **For updates**: confirm what changed (old value → new value where applicable).
- **For errors**: explain what was blocked and why, offer an alternative.

Do not dump raw JSON at the user. Extract and present only the fields relevant to their request.
