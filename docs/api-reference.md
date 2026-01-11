# Legato API Reference

## Pit API Endpoints

### Authentication

#### `GET /auth/login`
Display login page.

#### `GET /auth/github`
Initiate GitHub OAuth flow.

#### `GET /auth/github/callback`
Handle OAuth callback from GitHub.

#### `GET /auth/logout`
Log out current user and clear session.

---

### Dashboard

#### `GET /health`
Health check endpoint (no auth required).

**Response:**
```json
{
  "status": "healthy",
  "version": "1.0.0"
}
```

#### `GET /dashboard`
Main dashboard view (requires auth).

#### `GET /dashboard/api/status`
Dashboard data as JSON.

**Response:**
```json
{
  "recent_jobs": [...],
  "recent_artifacts": [...],
  "pending_agents": 3,
  "stats": {
    "total_entries": 150,
    "entries_this_week": 12
  }
}
```

---

### Motif (Transcript Intake)

#### `GET /dropbox`
Upload form page.

#### `POST /dropbox/upload`
Form-based transcript upload.

**Request (multipart/form-data):**
- `transcript` (text): Raw transcript content
- `file` (file): Alternative file upload (.txt, .md)

**Response:** Redirect to dashboard with flash message.

#### `POST /dropbox/api/upload`
JSON API for transcript upload.

**Request:**
```json
{
  "transcript": "Raw transcript text...",
  "source_id": "optional-source-identifier"
}
```

**Response:**
```json
{
  "status": "dispatched",
  "source": "dropbox-2026-01-10-143000"
}
```

---

### Library

#### `GET /library`
Browse library entries.

#### `GET /library/api/entries`
List entries with filtering.

**Query Parameters:**
- `category` (string): Filter by category
- `limit` (int): Max results (default 50)
- `offset` (int): Pagination offset

**Response:**
```json
{
  "entries": [
    {
      "entry_id": "kb-abc123",
      "title": "Entry Title",
      "category": "concept",
      "created_at": "2026-01-10T14:30:00Z"
    }
  ],
  "total": 150
}
```

#### `POST /library/api/search`
Search entries.

**Request:**
```json
{
  "query": "search terms",
  "category": "concept",
  "limit": 10,
  "semantic": true
}
```

**Response:**
```json
{
  "results": [
    {
      "entry_id": "kb-abc123",
      "title": "Entry Title",
      "similarity": 0.85,
      "snippet": "...matching content..."
    }
  ]
}
```

#### `GET /library/api/entry/{entry_id}`
Get full entry details.

**Response:**
```json
{
  "entry_id": "kb-abc123",
  "title": "Entry Title",
  "category": "concept",
  "content": "Full markdown content...",
  "domain_tags": ["ai", "mcp"],
  "created_at": "2026-01-10T14:30:00Z"
}
```

---

### Memory/Correlation API

#### `GET /memory/api/health`
RAG system health check.

**Response:**
```json
{
  "status": "healthy",
  "entries_indexed": 150,
  "embeddings_count": 150
}
```

#### `POST /memory/api/correlate`
Check semantic similarity for new content.

**Headers:**
- `Authorization: Bearer {SYSTEM_PAT}`

**Request:**
```json
{
  "title": "Content Title",
  "content": "Full content text...",
  "key_phrases": ["phrase1", "phrase2"],
  "needs_chord": false
}
```

**Response:**
```json
{
  "action": "CREATE",
  "score": 0.25,
  "matches": [
    {
      "entry_id": "kb-abc123",
      "title": "Similar Entry",
      "similarity": 0.45,
      "path": "concepts/2026-01-09-similar.md"
    }
  ],
  "recommendation": {
    "type": "CREATE_NEW",
    "reason": "No similar entries found above threshold"
  }
}
```

**Action Values:**
- `CREATE` - Create new entry (score < 0.70)
- `SUGGEST` - Human review needed (score 0.70-0.90)
- `APPEND` - Append to existing (score > 0.90)
- `QUEUE` - Queue as task on existing chord
- `SKIP` - Skip (near-duplicate)

---

### Agent Queue

#### `GET /agents`
Agent queue management page.

#### `POST /agents/api/queue-chord`
Queue an entry for chord creation.

**Headers:**
- `Authorization: Bearer {SYSTEM_PAT}`

**Request:**
```json
{
  "entry_id": "kb-abc123",
  "project_name": "project-slug",
  "project_type": "note"
}
```

#### `POST /agents/api/from-entry`
Create agent from library entry.

**Headers:**
- `Authorization: Bearer {SYSTEM_PAT}`

**Request:**
```json
{
  "entry_id": "kb-abc123",
  "project_name": "project-slug",
  "project_type": "note",
  "title": "Project Title",
  "description": "Project description..."
}
```

**Response:**
```json
{
  "status": "queued",
  "queue_id": "aq-abc123"
}
```

#### `POST /agents/api/{queue_id}/approve`
Approve pending agent.

**Request:**
```json
{
  "additional_comments": "Optional comments for kickoff"
}
```

**Response:**
```json
{
  "status": "approved",
  "queue_id": "aq-abc123"
}
```

#### `POST /agents/api/{queue_id}/reject`
Reject pending agent.

**Request:**
```json
{
  "reason": "Optional rejection reason"
}
```

#### `POST /agents/api/reject-all`
Reject all pending agents.

---

### Categories

#### `GET /categories/api/list`
List available categories.

**Response:**
```json
{
  "categories": [
    {
      "name": "epiphany",
      "display_name": "Epiphany",
      "description": "Major breakthrough or insight",
      "folder_name": "epiphanies",
      "count": 25
    }
  ]
}
```

#### `POST /categories/api/create`
Create new category.

**Request:**
```json
{
  "name": "custom-category",
  "display_name": "Custom Category",
  "description": "Description of this category"
}
```

---

### MCP Protocol

#### `HEAD /mcp`
Protocol version discovery.

**Response Headers:**
- `MCP-Protocol-Version: 2025-06-18`

#### `POST /mcp`
JSON-RPC 2.0 MCP handler.

**Headers:**
- `Authorization: Bearer {MCP_JWT_TOKEN}`
- `Content-Type: application/json`

**Available Methods:**

##### `initialize`
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {}
}
```

##### `tools/list`
```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/list"
}
```

##### `tools/call`
```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "search_library",
    "arguments": {
      "query": "search terms",
      "limit": 10
    }
  }
}
```

**Available Tools:**

| Tool | Description | Arguments |
|------|-------------|-----------|
| `search_library` | Semantic search | `query`, `limit`, `category` |
| `create_note` | Create new entry | `title`, `content`, `category` |
| `get_note` | Get full content | `entry_id` |
| `list_categories` | List categories | none |
| `list_recent_notes` | Recent entries | `limit`, `category` |

---

### OAuth Server

#### `GET /.well-known/oauth-authorization-server`
OAuth 2.1 server metadata (RFC 8414).

**Response:**
```json
{
  "issuer": "https://legato-pit.fly.dev",
  "authorization_endpoint": "https://legato-pit.fly.dev/oauth/authorize",
  "token_endpoint": "https://legato-pit.fly.dev/oauth/token",
  "registration_endpoint": "https://legato-pit.fly.dev/oauth/register",
  "response_types_supported": ["code"],
  "grant_types_supported": ["authorization_code", "refresh_token"],
  "code_challenge_methods_supported": ["S256"],
  "token_endpoint_auth_methods_supported": ["none"]
}
```

#### `POST /oauth/register`
Dynamic Client Registration (RFC 7591).

**Request:**
```json
{
  "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
  "client_name": "Claude",
  "token_endpoint_auth_method": "none"
}
```

**Response:**
```json
{
  "client_id": "mcp-abc123def456",
  "client_secret": null,
  "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
  "client_name": "Claude"
}
```

#### `GET /oauth/authorize`
Authorization endpoint.

**Query Parameters:**
- `client_id`: From DCR registration
- `redirect_uri`: Registered redirect URI
- `state`: CSRF token
- `code_challenge`: PKCE S256 challenge
- `response_type`: Must be "code"

#### `POST /oauth/token`
Token exchange endpoint.

**Request (application/x-www-form-urlencoded):**
- `grant_type`: "authorization_code" or "refresh_token"
- `code`: Authorization code (for auth code grant)
- `code_verifier`: PKCE verifier
- `redirect_uri`: Must match original
- `refresh_token`: For refresh grant

**Response:**
```json
{
  "access_token": "eyJ...",
  "token_type": "Bearer",
  "expires_in": 3600,
  "refresh_token": "..."
}
```

---

## Conduct → Pit Integration

### Transcript Dispatch

**Endpoint:** GitHub API `POST /repos/{org}/Legato.Conduct/dispatches`

**Payload:**
```json
{
  "event_type": "transcript-received",
  "client_payload": {
    "transcript": "Raw transcript text",
    "source": "dropbox-2026-01-10",
    "category_definitions": [...]
  }
}
```

### Agent Spawn Dispatch

**Endpoint:** GitHub API `POST /repos/{org}/Legato.Conduct/dispatches`

**Payload:**
```json
{
  "event_type": "spawn-agent",
  "client_payload": {
    "queue_id": "aq-abc123",
    "project_name": "project-slug",
    "project_type": "note",
    "signal_json": {
      "title": "Project Title",
      "intent": "Project intent...",
      "domain_tags": ["ai", "mcp"]
    }
  }
}
```

---

## Error Responses

All endpoints return errors in this format:

```json
{
  "error": "error_code",
  "error_description": "Human-readable description"
}
```

**Common Error Codes:**
- `unauthorized` - Missing or invalid authentication
- `invalid_request` - Malformed request
- `invalid_client` - Unknown OAuth client
- `invalid_grant` - Invalid or expired authorization code
- `server_error` - Internal server error
