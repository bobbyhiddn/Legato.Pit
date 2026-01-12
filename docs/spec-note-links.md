# Note Links Specification

**Version:** 1.0
**Created:** 2026-01-12
**Status:** Proposed

## Overview

This specification defines how explicit relationships between knowledge entries (notes) are created, stored, and propagated across the Legato ecosystem.

## Link Types

| Type | Direction | Meaning |
|------|-----------|---------|
| `related` | Bidirectional | General relationship between notes |
| `depends_on` | Directed | Source requires target to exist/be understood first |
| `blocks` | Directed | Source blocks progress on target |
| `implements` | Directed | Source implements the idea described in target |
| `references` | Directed | Source mentions or cites target |
| `contradicts` | Bidirectional | Notes present conflicting viewpoints |
| `supports` | Directed | Source provides evidence/support for target |

## Storage Architecture

### 1. Pit Database (Primary Store)

Links are stored in the `note_links` table:

```sql
CREATE TABLE note_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_entry_id TEXT NOT NULL,    -- e.g., 'kb-abc12345'
    target_entry_id TEXT NOT NULL,    -- e.g., 'kb-def67890'
    link_type TEXT DEFAULT 'related',
    description TEXT,                  -- Optional: why the link exists
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_by TEXT DEFAULT 'mcp-claude',
    UNIQUE(source_entry_id, target_entry_id, link_type)
);
```

### 2. Library Storage (Git-Native)

Links are propagated to the Library in two forms:

#### a) Frontmatter Links (Per-Note)

Each note includes its direct links in frontmatter:

```yaml
---
id: library.concept.async-patterns
title: "Async Patterns in Python"
category: concept
created: 2026-01-12T10:30:00Z
links:
  - target: kb-abc12345
    type: implements
    description: "Implements the concurrency model"
  - target: kb-def67890
    type: related
---
```

#### b) Links Index (Repository-Wide)

A `_meta/links.json` file maintains the complete link graph:

```json
{
  "version": 1,
  "updated_at": "2026-01-12T15:30:00Z",
  "links": [
    {
      "source": "kb-abc12345",
      "target": "kb-def67890",
      "type": "related",
      "description": null,
      "created_at": "2026-01-12T10:30:00Z"
    }
  ]
}
```

## Propagation Rules

### Pit → Library

When links are created/deleted in Pit, they should be synced to Library:

1. **On `link_notes` tool call:**
   - Insert into `note_links` table
   - Queue frontmatter update for affected notes
   - Queue `_meta/links.json` update

2. **Sync Frequency:**
   - Real-time for critical links (implements, blocks)
   - Batched (every 5 minutes) for general links

### Library → Pit

When the Library is synced to Pit:

1. **On sync:**
   - Parse frontmatter links from all notes
   - Parse `_meta/links.json` if exists
   - Merge with existing Pit links (prefer newer timestamps)
   - Flag conflicts for human review

### Conduct Integration

When Conduct processes transcripts:

1. **Auto-Link Detection:**
   - If transcript mentions an existing note by title, create `references` link
   - If new note is semantically similar (>0.85) to existing, suggest `related` link

2. **Project Spawning:**
   - When spawning a project, all linked notes should be included in context
   - `depends_on` links become dependencies in project planning

## MCP Tools

### `link_notes`

Create a link between two notes:

```json
{
  "tool": "link_notes",
  "arguments": {
    "source_id": "kb-abc12345",
    "target_id": "kb-def67890",
    "link_type": "implements",
    "description": "This concept implements the pattern"
  }
}
```

### `get_note_context`

Get a note with all its relationships:

```json
{
  "tool": "get_note_context",
  "arguments": {
    "entry_id": "kb-abc12345",
    "include_semantic": true,
    "semantic_limit": 5
  }
}
```

Returns:
- The note itself
- Outgoing links (this note → others)
- Incoming links (others → this note)
- Semantic neighbors (similar content)
- Related projects

## Bidirectional Link Handling

For symmetric link types (`related`, `contradicts`):
- Creating A → B automatically creates B → A
- Deleting A → B automatically deletes B → A

For directed link types:
- Only the specified direction is created
- Inverse lookups use `incoming` vs `outgoing` queries

## Conflict Resolution

When sync detects conflicting link states:

1. **Same link, different types:** Prefer Pit (more recent)
2. **Link exists in one, not other:** Create in both
3. **Link deleted in one, exists in other:**
   - If deleted recently (< 24h), propagate deletion
   - Otherwise, flag for human review

## Future Considerations

1. **Link Strength/Weight:** Add confidence scores to links
2. **Temporal Links:** Links that expire or have time bounds
3. **Cross-Repo Links:** Links to entries in other Library forks
4. **Link Visualization:** Graph view in Pit dashboard (partially implemented)
