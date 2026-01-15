# Tasks Specification

**Version:** 1.0
**Created:** 2026-01-15
**Status:** Active

## Overview

This specification defines how notes are marked as tasks in the Legato ecosystem. Tasks are notes with actionable status tracking, enabling project management workflows alongside knowledge capture.

## Core Principle

**Tasks are frontmatter-first.** The `task_status` field is a frontmatter attribute that:
1. Is visible when models read note content
2. Can be set at creation time via `create_note`
3. Syncs bidirectionally between Library (git) and Pit (database)

## Frontmatter Schema

### Standard Note (Non-Task)

```yaml
---
id: library.concept.my-concept
title: "My Concept"
category: concept
created: 2026-01-15T10:00:00Z
source: mcp-claude
domain_tags: []
key_phrases: []
---

Content here...
```

### Task Note

```yaml
---
id: library.reminder.fix-auth-bug
title: "Fix authentication timeout bug"
category: reminder
created: 2026-01-15T10:00:00Z
source: mcp-claude
domain_tags: []
key_phrases: []
task_status: pending
due_date: 2026-01-20
---

Content here...
```

## Task Status Values

| Status | Meaning | UI Color |
|--------|---------|----------|
| `pending` | Not yet started | Blue |
| `in_progress` | Currently being worked on | Orange |
| `blocked` | Waiting on external dependency | Red |
| `done` | Completed | Green |

## Task Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `task_status` | string | Yes (for tasks) | One of: `pending`, `in_progress`, `blocked`, `done` |
| `due_date` | string (ISO date) | No | Optional deadline in `YYYY-MM-DD` format |

## Creating Tasks

### Via MCP `create_note` Tool

Models can create tasks directly by including `task_status` in the create_note call:

```json
{
  "tool": "create_note",
  "arguments": {
    "title": "Fix authentication timeout bug",
    "content": "The login flow times out after 30 seconds...",
    "category": "reminder",
    "task_status": "pending",
    "due_date": "2026-01-20"
  }
}
```

**Response:**
```json
{
  "success": true,
  "entry_id": "kb-a1b2c3d4",
  "title": "Fix authentication timeout bug",
  "category": "reminder",
  "file_path": "reminders/2026-01-15-fix-authentication-timeout-bug.md",
  "task_status": "pending",
  "due_date": "2026-01-20"
}
```

### Via MCP `update_task_status` Tool

Convert an existing note to a task, or update task status:

```json
{
  "tool": "update_task_status",
  "arguments": {
    "entry_id": "kb-a1b2c3d4",
    "status": "in_progress",
    "due_date": "2026-01-22"
  }
}
```

## Reading Tasks

### Via MCP `list_tasks` Tool

```json
{
  "tool": "list_tasks",
  "arguments": {
    "status": "pending",
    "due_before": "2026-01-31",
    "limit": 20
  }
}
```

### Via MCP `get_note` Tool

When reading any note, task fields are included:

```json
{
  "tool": "get_note",
  "arguments": {
    "entry_id": "kb-a1b2c3d4"
  }
}
```

**Response includes:**
```json
{
  "entry_id": "kb-a1b2c3d4",
  "title": "Fix authentication timeout bug",
  "content": "---\nid: library.reminder.fix-auth...\n---\n\nThe login flow...",
  "task_status": "pending",
  "due_date": "2026-01-20"
}
```

## Storage Architecture

### 1. Library Storage (Git-Native, Source of Truth)

Task status lives in frontmatter, synced via git:

```yaml
---
id: library.reminder.fix-auth-bug
title: "Fix authentication timeout bug"
task_status: pending
due_date: 2026-01-20
---
```

### 2. Pit Database (Indexed Cache)

The `knowledge_entries` table includes task columns for fast queries:

```sql
-- Task-related columns in knowledge_entries
task_status TEXT,    -- NULL for non-tasks, else: pending|in_progress|blocked|done
due_date DATE        -- Optional deadline
```

**Index for fast task queries:**
```sql
CREATE INDEX idx_knowledge_task_status ON knowledge_entries(task_status);
```

## Sync Behavior

### Library → Pit (on sync)

When parsing frontmatter:
1. Extract `task_status` if present
2. Extract `due_date` if present
3. Store in database columns

### Pit → Library (on update)

When `update_task_status` is called:
1. Update database columns
2. Update frontmatter in Library file
3. Commit change to git

## Task Ordering (UI)

Tasks are displayed in priority order:

1. **Blocked** (priority 0) - Needs attention
2. **In Progress** (priority 1) - Active work
3. **Pending** (priority 2) - Ready to start
4. **Done** (priority 3) - Completed

Within each status, sort by:
1. `due_date` ascending (NULL last)
2. `updated_at` descending

## Task Links

Tasks can use note links for dependency tracking:

| Link Type | Task Usage |
|-----------|------------|
| `depends_on` | This task requires another to complete first |
| `blocks` | This task blocks another from starting |
| `implements` | This task implements an idea from another note |
| `related` | General relationship |

Example workflow:
```json
// Task A depends on Task B
{
  "tool": "link_notes",
  "arguments": {
    "source_id": "kb-taskA",
    "target_id": "kb-taskB",
    "link_type": "depends_on"
  }
}
```

## Model Guidelines

When creating tasks, models should:

1. **Use appropriate categories:**
   - `reminder` - Personal tasks, things to remember
   - `worklog` - Track work to be done (or completed)
   - Any category can have tasks, but these are most common

2. **Set realistic due dates:**
   - Only set `due_date` if there's a genuine deadline
   - Use ISO format: `YYYY-MM-DD`

3. **Write actionable titles:**
   - Good: "Fix authentication timeout bug"
   - Bad: "Authentication issue"

4. **Include context in content:**
   - Describe the task clearly
   - Include acceptance criteria if applicable
   - Link to related notes

## Removing Task Status

To convert a task back to a regular note, set `task_status` to `null` or omit it from frontmatter. The MCP does not currently expose a "clear task status" operation - this is intentional to prevent accidental task loss.

## Future Considerations

1. **Task Templates:** Pre-defined task structures for common workflows
2. **Recurring Tasks:** Tasks that regenerate on completion
3. **Task Estimates:** Time/effort estimates for planning
4. **Task Assignments:** Multi-user task ownership
5. **Task History:** Audit log of status changes
