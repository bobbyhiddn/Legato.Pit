# Legato Documentation

Comprehensive architecture and operational documentation for the Legato ecosystem.

## Contents

| Document | Description |
|----------|-------------|
| [Architecture Overview](architecture.md) | System components, interactions, and design decisions |
| [Data Flows](data-flows.md) | End-to-end data flow diagrams and examples |
| [API Reference](api-reference.md) | API contracts between components |
| [Deployment Guide](deployment.md) | Deployment architecture and configuration |

## Quick Start

Legato transforms voice transcripts into structured knowledge and executable projects:

```
Voice Transcript → Classification → Knowledge Library → Project Spawning → Autonomous Implementation
```

### Core Components

- **Legato.Pit** - Web dashboard, MCP server, OAuth, RAG system
- **Legato.Conduct** - Orchestrator, classification pipeline, GitHub Actions
- **Legato.Library** - Git-native knowledge repository
- **Legato.Lab/*** - Spawned project repositories

### Key Flows

1. **Motif Intake** - User uploads transcript via Pit dropbox
2. **Classification** - Conduct parses and classifies threads
3. **Correlation** - Pit checks semantic similarity to existing entries
4. **Routing** - Create new entries, append to existing, or queue projects
5. **Spawning** - Human-approved projects become Lab repositories
6. **Implementation** - GitHub Copilot implements assigned issues
