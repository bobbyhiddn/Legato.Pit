# Legato Architecture

## System Overview

Legato is an end-to-end system that transforms voice transcripts into structured knowledge artifacts and executable projects. The ecosystem consists of three core repositories orchestrated through GitHub Actions.

```mermaid
graph TB
    subgraph External["External Integrations"]
        Claude["Claude.ai<br/>(MCP Connector)"]
        Copilot["GitHub Copilot<br/>(Autonomous Coding)"]
    end

    subgraph Legato["Legato Ecosystem"]
        Pit["Legato.Pit<br/>━━━━━━━━━━━<br/>Dashboard<br/>MCP Server<br/>OAuth<br/>RAG System<br/>Agent Queue"]

        Conduct["Legato.Conduct<br/>━━━━━━━━━━━━━<br/>Orchestrator<br/>Classification<br/>Routing<br/>GitHub Actions"]

        Library["Legato.Library<br/>━━━━━━━━━━━━<br/>Knowledge Store<br/>Git-native<br/>Markdown artifacts"]

        Lab["Legato.Lab/*<br/>━━━━━━━━━━<br/>Project Repos<br/>.Note (simple)<br/>.Chord (complex)"]
    end

    Claude -->|"MCP JSON-RPC"| Pit
    Pit -->|"repository_dispatch"| Conduct
    Conduct -->|"Git commits"| Library
    Conduct -->|"Spawn repos"| Lab
    Lab -->|"Issue assigned"| Copilot
    Copilot -->|"Creates PR"| Lab
    Pit -->|"Sync & index"| Library
    Conduct -->|"Correlation check"| Pit
```

## Component Details

### Legato.Pit - Web Dashboard & MCP Server

The central hub for user interaction and system orchestration.

```mermaid
graph LR
    subgraph Pit["Legato.Pit"]
        subgraph Web["Web Interface"]
            Dashboard["Dashboard"]
            Motif["Motif Dropbox"]
            LibBrowser["Library Browser"]
            Agents["Agent Queue"]
        end

        subgraph API["API Layer"]
            OAuth["OAuth Server"]
            MCP["MCP Server"]
            Memory["Memory API"]
        end

        subgraph Data["Data Layer"]
            RAG["RAG System"]
            DB[(SQLite DBs)]
        end
    end

    Web --> API
    API --> Data
```

**Key Modules:**

| Module | Purpose |
|--------|---------|
| `core.py` | Flask app factory, blueprint registration |
| `auth.py` | GitHub OAuth authentication |
| `dashboard.py` | System status and monitoring |
| `dropbox.py` | Transcript intake (Motif) |
| `library.py` | Knowledge browser and search |
| `mcp_server.py` | Claude.ai MCP protocol handler |
| `oauth_server.py` | OAuth 2.1 with Dynamic Client Registration |
| `agents.py` | Project queue and approval workflow |
| `memory_api.py` | Semantic correlation API |
| `rag/*` | Embeddings, search, GitHub sync |

### Legato.Conduct - Orchestrator

Stateless orchestrator running as GitHub Actions workflows.

```mermaid
graph TB
    subgraph Conduct["Legato.Conduct"]
        subgraph Workflows["GitHub Actions"]
            Process["process-transcript.yml"]
            Spawn["spawn-from-pit.yml"]
            Continue["process-transcript-continue.yml"]
        end

        subgraph Package["Python Package"]
            Classifier["classifier.py"]
            Knowledge["knowledge.py"]
            Projects["projects.py"]
        end
    end

    Process --> Classifier
    Process --> Knowledge
    Process --> Projects
    Spawn --> Projects
```

**Key Modules:**

| Module | Purpose |
|--------|---------|
| `classifier.py` | Parse transcripts, classify threads, check correlation |
| `knowledge.py` | Extract and commit knowledge artifacts |
| `projects.py` | Spawn Lab repositories, create issues |

### Legato.Library - Knowledge Repository

Git-native storage for all knowledge artifacts.

```mermaid
graph TB
    subgraph Library["Legato.Library"]
        subgraph Categories["Knowledge Categories"]
            Epiphanies["epiphanies/"]
            Concepts["concepts/"]
            Reflections["reflections/"]
            Glimmers["glimmers/"]
            Reminders["reminders/"]
            Worklog["worklog/"]
        end

        Index["index.json"]
        Workflows[".github/workflows/"]
    end
```

**Artifact Structure:**

```yaml
---
id: library.{category}.{slug}
title: "Artifact Title"
category: epiphany|concept|reflection|glimmer|reminder|worklog
created: 2026-01-07T15:30:00Z
source_transcript: transcript-2026-01-07
domain_tags: [ai, architecture]
key_phrases: ["key term", "another term"]
needs_chord: false
chord_status: null
---

# Content in markdown...
```

### Legato.Lab/* - Project Repositories

Spawned repositories for implementation projects.

```mermaid
graph LR
    subgraph Lab["Lab Repositories"]
        Note["Lab.project.Note<br/>━━━━━━━━━━━━━<br/>Simple projects<br/>Single PR scope"]

        Chord["Lab.project.Chord<br/>━━━━━━━━━━━━━━<br/>Complex projects<br/>Multi-phase PRs"]
    end

    Template[".Note/.Chord Templates"] --> Note
    Template --> Chord
```

**Repository Structure:**

```
Lab.project.Note/
├── .github/workflows/
│   └── on-issue-assigned.yml
├── README.md
├── SIGNAL.md              # Project intent
├── copilot-instructions.md
├── plans/
└── src/
```

## Design Decisions

### Everything Starts as Knowledge

All threads are classified as `KNOWLEDGE` first. Projects are escalated via the `needs_chord` flag.

```mermaid
flowchart LR
    Transcript --> Classification
    Classification --> Knowledge["Knowledge Entry<br/>(always created)"]
    Knowledge -->|"needs_chord=true"| Queue["Agent Queue"]
    Queue -->|"Human Approval"| Project["Lab Repository"]
```

**Rationale:** Ensures all insights are captured in the Library before any implementation begins.

### Git-Native Architecture

- No database for core artifacts (only GitHub)
- Full version history via git
- Changes visible through pull requests
- Easy to fork, replicate, audit

### Correlation Before Action

Semantic similarity is checked before creating new entries:

```mermaid
flowchart TD
    NewContent["New Content"] --> Correlate["Check Correlation"]
    Correlate --> Score{"Similarity Score"}
    Score -->|"< 0.70"| Create["CREATE new entry"]
    Score -->|"0.70 - 0.90"| Suggest["SUGGEST (human review)"]
    Score -->|"> 0.90"| Append["APPEND to existing"]
```

### Human-in-the-Loop for Projects

Projects require explicit human approval before spawning:

```mermaid
sequenceDiagram
    participant C as Conduct
    participant P as Pit
    participant U as User
    participant L as Lab

    C->>P: Queue agent (needs_chord=true)
    P->>U: Show in /agents dashboard
    U->>P: Click "Approve"
    P->>C: Dispatch spawn-agent
    C->>L: Create repository
    C->>L: Assign to Copilot
```

## Database Schema

### Pit Databases

```mermaid
erDiagram
    knowledge_entries {
        int id PK
        string entry_id UK
        string title
        string category
        text content
        string file_path
        bool needs_chord
        string chord_status
        string embedding_id
    }

    embeddings {
        int id PK
        string entry_id FK
        blob embedding
        string model
    }

    agent_queue {
        int id PK
        string queue_id UK
        string project_name
        string status
        string related_entry_id
        string approved_by
    }

    oauth_clients {
        int id PK
        string client_id UK
        string client_name
        text redirect_uris
    }

    knowledge_entries ||--o{ embeddings : has
    knowledge_entries ||--o| agent_queue : spawns
```

## Security Model

```mermaid
flowchart TB
    subgraph Auth["Authentication"]
        OAuth["GitHub OAuth 2.0"]
        JWT["JWT Tokens (MCP)"]
        PAT["GitHub PATs"]
    end

    subgraph Authz["Authorization"]
        Allowlist["User Allowlist"]
        Scopes["Token Scopes"]
    end

    OAuth --> Allowlist
    JWT --> Scopes
    PAT --> Scopes
```

**Key Security Features:**

- GitHub OAuth with PKCE for web authentication
- JWT tokens for MCP (never exposes GitHub tokens to Claude)
- User allowlist (`GH_ALLOWED_USERS`)
- Separate PATs for different operations (Library, Lab, Conduct)
