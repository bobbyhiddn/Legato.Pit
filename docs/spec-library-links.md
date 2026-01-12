# Library Link Storage Specification

**Version:** 1.0
**Created:** 2026-01-12
**Status:** Proposed
**Depends On:** spec-note-links.md

## Overview

This specification defines how note links are stored in the Legato.Library Git repository, enabling Git-native link tracking alongside the markdown knowledge artifacts.

## Storage Format

### 1. Per-Note Links (Frontmatter)

Each note includes its direct outgoing links in YAML frontmatter:

```yaml
---
id: library.concept.async-patterns
title: "Async Patterns in Python"
category: concept
created: 2026-01-12T10:30:00Z
source: mcp-claude
domain_tags: [python, async, concurrency]
key_phrases: ["async/await", "coroutines"]

# NEW: Links section
links:
  - target: kb-abc12345
    type: implements
    description: "Implements the concurrency model described here"
  - target: kb-def67890
    type: related
  - target: kb-ghi78901
    type: depends_on
    description: "Requires understanding of this note first"
---

# Async Patterns in Python

Content here...
```

#### Link Object Schema

```yaml
links:
  - target: string      # Required: target entry_id (kb-XXXXXXXX)
    type: string        # Required: link type (see spec-note-links.md)
    description: string # Optional: why this link exists
    created_at: string  # Optional: ISO timestamp (added by sync)
```

### 2. Links Index (`_meta/links.json`)

A repository-wide index of all links for efficient querying:

```json
{
  "version": 1,
  "schema": "legato-links-v1",
  "updated_at": "2026-01-12T15:30:00Z",
  "entry_count": 142,
  "link_count": 287,
  "links": [
    {
      "id": "link-001",
      "source": "kb-abc12345",
      "target": "kb-def67890",
      "type": "related",
      "description": null,
      "created_at": "2026-01-12T10:30:00Z",
      "created_by": "mcp-claude"
    },
    {
      "id": "link-002",
      "source": "kb-def67890",
      "target": "kb-abc12345",
      "type": "related",
      "description": null,
      "created_at": "2026-01-12T10:30:00Z",
      "created_by": "mcp-claude"
    }
  ],
  "stats": {
    "by_type": {
      "related": 120,
      "implements": 45,
      "references": 82,
      "depends_on": 25,
      "supports": 10,
      "contradicts": 3,
      "blocks": 2
    },
    "orphaned_targets": [],
    "most_linked": [
      {"entry_id": "kb-abc12345", "count": 15},
      {"entry_id": "kb-def67890", "count": 12}
    ]
  }
}
```

### 3. Directory Structure

```
Legato.Library/
├── concepts/
│   ├── 2026-01-10-async-patterns.md     # Has links in frontmatter
│   └── 2026-01-11-error-handling.md
├── epiphanys/
│   └── 2026-01-12-breakthrough.md
├── reflections/
│   └── ...
├── _meta/
│   ├── links.json                        # Complete link index
│   ├── entries.json                      # Entry index (existing)
│   └── schema.json                       # Schema definitions
└── .github/
    └── workflows/
        └── validate-links.yml            # Link validation workflow
```

## Sync Behavior

### Pit → Library Sync

When Pit pushes links to Library:

1. **Update affected note frontmatter:**
   ```python
   def update_note_links(entry_id: str, links: list[dict]):
       # Fetch current note content
       # Parse frontmatter
       # Update 'links' array
       # Commit changes
   ```

2. **Update `_meta/links.json`:**
   ```python
   def update_links_index(new_links: list[dict]):
       # Fetch current index
       # Merge new links (by source+target+type)
       # Update stats
       # Commit changes
   ```

### Library → Pit Sync

When Pit syncs from Library:

1. **Parse frontmatter links from all notes**
2. **Parse `_meta/links.json`**
3. **Merge into Pit database:**
   - Prefer Library version if timestamps differ
   - Flag conflicts for human review

## Validation

### GitHub Actions Workflow

```yaml
# .github/workflows/validate-links.yml
name: Validate Links

on:
  push:
    paths:
      - '**/*.md'
      - '_meta/links.json'

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Validate link targets exist
        run: |
          python scripts/validate_links.py

      - name: Check for orphaned links
        run: |
          python scripts/check_orphans.py

      - name: Verify frontmatter/index consistency
        run: |
          python scripts/verify_consistency.py
```

### Validation Rules

1. **Target Exists:** All `target` entry_ids must exist in the repository
2. **Bidirectional Consistency:** For symmetric types, both directions must exist
3. **Frontmatter/Index Match:** Links in frontmatter must appear in index
4. **No Self-Links:** `source` != `target`
5. **Valid Types:** All `type` values must be in allowed set

## Migration

### Adding Links to Existing Notes

For notes created before link support:

1. **No links array:** Treated as having zero links
2. **On first link creation:** Add `links` array to frontmatter
3. **Backward compatible:** Old parsers ignore unknown frontmatter fields

### Index Creation

If `_meta/links.json` doesn't exist:

1. Scan all notes for frontmatter links
2. Generate index from discovered links
3. Commit new index file

## Frontmatter Examples

### Note with Multiple Link Types

```yaml
---
id: library.concept.microservices-auth
title: "Microservices Authentication Patterns"
category: concept
created: 2026-01-12T10:30:00Z
links:
  - target: kb-oauth2-deep
    type: implements
    description: "Implements OAuth2 patterns for service-to-service auth"
  - target: kb-api-gateway
    type: depends_on
    description: "Requires API gateway to be configured first"
  - target: kb-session-mgmt
    type: contradicts
    description: "Takes different approach than session-based auth"
  - target: kb-jwt-intro
    type: references
  - target: kb-security-best
    type: supports
---
```

### Note with Auto-Detected Links

```yaml
---
id: library.reflection.project-learnings
title: "Project Learnings"
category: reflection
created: 2026-01-12T14:00:00Z
links:
  - target: kb-abc12345
    type: references
    auto_detected: true
    match_type: entry_id
  - target: kb-def67890
    type: references
    auto_detected: true
    match_type: title
---

Today I applied concepts from kb-abc12345 to the project.
The Async Patterns note was particularly helpful.
```

## Query Patterns

### Find All Links for a Note

```python
# From frontmatter
def get_outgoing_links(entry_id: str) -> list[dict]:
    note = fetch_note(entry_id)
    return note.frontmatter.get('links', [])

# From index (includes incoming)
def get_all_links(entry_id: str) -> dict:
    index = fetch_links_index()
    return {
        'outgoing': [l for l in index['links'] if l['source'] == entry_id],
        'incoming': [l for l in index['links'] if l['target'] == entry_id]
    }
```

### Find Most Connected Notes

```python
def get_most_connected(limit: int = 10) -> list[dict]:
    index = fetch_links_index()
    return index['stats']['most_linked'][:limit]
```

### Find Orphaned Links

```python
def find_orphaned_links() -> list[dict]:
    index = fetch_links_index()
    entries = fetch_all_entry_ids()
    return [
        l for l in index['links']
        if l['target'] not in entries
    ]
```

## Performance Considerations

1. **Index Size:** For large libraries (>1000 notes), the index may grow large
   - Consider pagination or chunking for very large libraries
   - Stats section provides summary without parsing full list

2. **Sync Frequency:** Don't update index on every link change
   - Batch updates during sync operations
   - Real-time updates only for critical links

3. **Frontmatter Overhead:** Links in frontmatter add parsing overhead
   - Keep descriptions concise
   - Consider moving verbose descriptions to index only
