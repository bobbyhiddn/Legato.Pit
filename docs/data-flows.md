# Legato Data Flows

## End-to-End Pipeline

```mermaid
sequenceDiagram
    participant U as User
    participant P as Pit
    participant C as Conduct
    participant L as Library
    participant Lab as Lab Repo
    participant Co as Copilot

    U->>P: Upload transcript (Motif)
    P->>C: Dispatch transcript
    C->>C: Parse into threads
    C->>C: Classify each thread
    C->>P: Check correlation
    P-->>C: Return matches + action
    C->>L: Commit knowledge entries

    alt needs_chord = true
        C->>P: Queue agent
        U->>P: Approve in dashboard
        P->>C: Dispatch spawn-agent
        C->>Lab: Create repository
        C->>Lab: Create issue
        C->>Lab: Assign to Copilot
        Co->>Lab: Create PR
        U->>Lab: Review & merge
    end
```

## Flow 1: Transcript Intake

User uploads a transcript through the Motif dropbox.

```mermaid
sequenceDiagram
    participant U as User
    participant D as Dropbox Route
    participant G as GitHub API
    participant C as Conduct

    U->>D: POST /dropbox/upload
    D->>D: Validate & sanitize
    D->>D: Load category definitions
    D->>G: POST /repos/{org}/Conduct/dispatches
    Note right of G: event_type: transcript-received
    G->>C: Trigger workflow
    D-->>U: Redirect to dashboard
```

**Dispatch Payload:**
```json
{
  "event_type": "transcript-received",
  "client_payload": {
    "transcript": "Raw transcript text...",
    "source": "dropbox-2026-01-10",
    "category_definitions": [
      {"name": "epiphany", "display_name": "Epiphany", ...}
    ]
  }
}
```

## Flow 2: Classification Pipeline

Conduct processes the transcript through parsing, classification, and routing.

```mermaid
flowchart TB
    subgraph Parse["Phase 1: Parse"]
        T["Raw Transcript"] --> Claude1["Claude API"]
        Claude1 --> Threads["threads.json"]
    end

    subgraph Classify["Phase 2: Classify"]
        Threads --> Claude2["Claude API"]
        Claude2 --> Classified["Classified Threads"]
        Classified --> Correlate["Correlation Check"]
        Correlate --> Pit["Pit /memory/api/correlate"]
        Pit --> Routing["routing.json"]
    end

    subgraph Route["Phase 3: Route"]
        Routing --> Decision{"Action?"}
        Decision -->|CREATE| Library["Commit to Library"]
        Decision -->|APPEND| Append["Append to existing"]
        Decision -->|QUEUE| Queue["Queue agent in Pit"]
        Decision -->|SKIP| Skip["Skip (duplicate)"]
    end
```

**Classification Output:**
```json
{
  "id": "thread-001",
  "type": "KNOWLEDGE",
  "knowledge_category": "concept",
  "knowledge_title": "MCP Protocol Overview",
  "needs_chord": false,
  "domain_tags": ["mcp", "ai", "protocol"],
  "correlation_score": 0.25,
  "correlation_action": "CREATE"
}
```

## Flow 3: Correlation Check

Conduct checks semantic similarity against existing Library entries.

```mermaid
sequenceDiagram
    participant C as Conduct
    participant P as Pit Memory API
    participant E as Embeddings DB
    participant O as OpenAI

    C->>P: POST /memory/api/correlate
    Note right of C: title, content, key_phrases
    P->>O: Generate embedding
    O-->>P: Vector embedding
    P->>E: Cosine similarity search
    E-->>P: Top-k matches
    P->>P: Determine action
    P-->>C: Response with recommendation

    alt score < 0.70
        Note right of P: Action: CREATE
    else score 0.70-0.90
        Note right of P: Action: SUGGEST (review)
    else score > 0.90
        Note right of P: Action: APPEND
    end
```

**Correlation Request:**
```json
{
  "title": "MCP Protocol Overview",
  "content": "Full content text...",
  "key_phrases": ["mcp server", "json-rpc"],
  "needs_chord": false
}
```

**Correlation Response:**
```json
{
  "action": "CREATE",
  "score": 0.25,
  "matches": [
    {
      "entry_id": "kb-abc123",
      "title": "Similar Entry",
      "similarity": 0.45
    }
  ],
  "recommendation": {
    "type": "CREATE_NEW",
    "reason": "No similar entries found"
  }
}
```

## Flow 4: Knowledge Commit

Classified threads become artifacts in the Library.

```mermaid
sequenceDiagram
    participant C as Conduct
    participant G as GitHub API
    participant L as Library Repo

    C->>C: Extract knowledge artifact
    C->>C: Generate entry_id (kb-XXXXXXXX)
    C->>C: Build YAML frontmatter
    C->>C: Format markdown content
    C->>G: PUT /repos/.../contents/{path}
    Note right of G: Base64 encoded content
    G->>L: Commit file
    G-->>C: Commit SHA

    Note over L: File created at:<br/>concepts/2026-01-10-mcp-protocol.md
```

**Artifact Format:**
```markdown
---
id: library.concept.mcp-protocol
title: "MCP Protocol Overview"
category: concept
created: 2026-01-10T14:30:00Z
source_transcript: dropbox-2026-01-10
domain_tags: [mcp, ai, protocol]
key_phrases: [mcp server, json-rpc]
needs_chord: false
---

# MCP Protocol Overview

Content extracted from transcript...
```

## Flow 5: Agent Queue & Approval

Threads with `needs_chord=true` go through human approval.

```mermaid
sequenceDiagram
    participant C as Conduct
    participant P as Pit
    participant U as User
    participant A as Agent Queue

    C->>P: POST /agents/api/from-entry
    Note right of C: entry_id, project_name, type
    P->>A: Insert queue record
    A-->>P: queue_id
    P-->>C: Queued response

    U->>P: GET /agents (dashboard)
    P->>A: Fetch pending agents
    A-->>P: List of pending
    P-->>U: Render queue UI

    U->>P: POST /agents/api/{id}/approve
    P->>A: Update status → approved
    P->>C: Dispatch spawn-agent
```

**Queue Entry:**
```json
{
  "queue_id": "aq-abc123",
  "project_name": "mcp-bedrock-adapter",
  "project_type": "note",
  "title": "MCP Bedrock Adapter",
  "description": "Wraps AWS Bedrock...",
  "related_entry_id": "kb-xyz789",
  "status": "pending"
}
```

## Flow 6: Project Spawning

Approved projects become Lab repositories.

```mermaid
sequenceDiagram
    participant P as Pit
    participant C as Conduct
    participant G as GitHub API
    participant L as Lab Repo
    participant Co as Copilot

    P->>C: Dispatch spawn-agent
    Note right of P: project_name, type, signal

    C->>G: Create repository from template
    G->>L: Initialize repo
    G-->>C: Repo created

    C->>L: Write SIGNAL.md
    C->>L: Create issue #1
    Note right of L: Tasker template body

    C->>G: GraphQL: Assign @copilot-swe-agent
    G->>Co: Issue assigned
    Co->>L: Creates implementation PR
```

**SIGNAL.md:**
```markdown
# MCP Bedrock Adapter

## Intent
MCP server that wraps AWS Bedrock API for JWICS environments.

## Domain Tags
mcp, aws, bedrock, classified

## Source
- Transcript: dropbox-2026-01-10
- Library Entry: kb-xyz789

## Related
- [MCP Protocol Overview](../../Legato.Library/concepts/...)
```

## Flow 7: Library Sync (Background)

Pit continuously syncs with Library for RAG and correlation.

```mermaid
sequenceDiagram
    participant S as Sync Thread
    participant G as GitHub API
    participant L as Library
    participant O as OpenAI
    participant D as legato.db

    loop Every 60 seconds (while active)
        S->>G: Clone/pull Library
        G->>L: Fetch latest
        L-->>S: Repository contents

        loop For each artifact
            S->>S: Parse frontmatter
            S->>S: Extract content
            S->>O: Generate embedding
            O-->>S: Vector embedding
            S->>D: Upsert entry + embedding
        end
    end
```

## Flow 8: MCP Integration (Claude.ai)

Claude.ai connects via MCP to search and create knowledge.

```mermaid
sequenceDiagram
    participant C as Claude.ai
    participant O as OAuth Server
    participant M as MCP Server
    participant R as RAG System
    participant G as GitHub

    C->>O: Discover /.well-known/oauth-authorization-server
    C->>O: POST /oauth/register (DCR)
    O-->>C: client_id

    C->>O: GET /oauth/authorize
    O->>G: Redirect to GitHub OAuth
    G-->>O: Authorization code
    O->>C: Redirect with auth code

    C->>O: POST /oauth/token
    O-->>C: JWT access token

    C->>M: POST /mcp (tools/call)
    Note right of C: search_library query
    M->>R: Semantic search
    R-->>M: Results
    M-->>C: Tool response
```

**MCP Tool Call:**
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "search_library",
    "arguments": {
      "query": "MCP protocol implementation",
      "limit": 10
    }
  }
}
```

## Flow 9: Chord Grouping

Related chord candidates are grouped into multi-note chords.

```mermaid
flowchart TB
    subgraph Input["Transcript with Multiple Ideas"]
        T1["Thread 1: Auth system"]
        T2["Thread 2: Token validation"]
        T3["Thread 3: User permissions"]
    end

    subgraph Classify["Classification"]
        T1 --> C1["needs_chord=true<br/>tags: auth, security"]
        T2 --> C2["needs_chord=true<br/>tags: auth, tokens"]
        T3 --> C3["needs_chord=true<br/>tags: auth, permissions"]
    end

    subgraph Group["Tag Similarity Grouping"]
        C1 --> G["Common tag: auth"]
        C2 --> G
        C3 --> G
        G --> Chord["Single 3-note Chord"]
    end

    subgraph Spawn["Spawning"]
        Chord --> Repo["Lab.auth-system.Chord"]
        Repo --> Issue["Issue with 3 linked notes"]
    end
```

## Flow 10: Append to Existing

When correlation finds a near-duplicate, content is appended.

```mermaid
sequenceDiagram
    participant C as Conduct
    participant P as Pit
    participant L as Library

    C->>P: POST /memory/api/correlate
    Note right of C: New content about MCP
    P-->>C: score: 0.88, action: APPEND
    Note right of P: Match: existing MCP note

    C->>C: Format append content
    C->>L: GET existing file
    L-->>C: Current content
    C->>C: Merge new content
    C->>L: PUT updated file
    Note right of L: Appended section added
```

**Appended Format:**
```markdown
---
# ... existing frontmatter ...
updated: 2026-01-11T10:00:00Z
---

# Original Title

Original content...

---

## Update: 2026-01-11

Additional insights from new transcript...
```
