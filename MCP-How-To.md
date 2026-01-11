# MCP Connector Setup for Claude.ai

This guide explains how to connect Legato.Pit to Claude.ai as a custom MCP connector.

## Prerequisites

1. **Pit must be deployed** with HTTPS (e.g., `https://pit.legato.dev`)
2. **GitHub OAuth configured** with `GH_OAUTH_CLIENT_ID` and `GH_OAUTH_CLIENT_SECRET`
3. **PyJWT installed**: `pip install PyJWT>=2.8.0`

## How It Works

```
Claude.ai → Discovers OAuth endpoints → Registers via DCR → Redirects to GitHub
    ↓
You log in with GitHub → Pit issues JWT token → Claude can use MCP tools
```

Pit acts as an OAuth 2.1 Authorization Server with Dynamic Client Registration (DCR).
Claude never sees your GitHub token - Pit issues its own JWTs.

## Setup in Claude.ai

### 1. Open Claude Settings

Go to **Settings** → **Connectors**

### 2. Add Custom Connector

Click **"Add custom connector"** and enter:

```
Server URL: https://pit.legato.dev/mcp
```

Replace with your actual Pit deployment URL.

### 3. Authenticate

Claude will:
1. Discover OAuth metadata at `/.well-known/oauth-authorization-server`
2. Register itself via `/oauth/register` (Dynamic Client Registration)
3. Redirect you to GitHub for login
4. Exchange tokens and connect

### 4. Verify Connection

Once connected, you should see "legato-pit" in your connectors list with a green status.

## Available Tools

Once connected, Claude can use these tools:

| Tool | Description | Example Prompt |
|------|-------------|----------------|
| `search_library` | Semantic search | "Search my library for notes about AI agents" |
| `create_note` | Create a new note | "Create a concept note titled 'MCP Integration' with..." |
| `get_note` | Get full note content | "Show me the full content of note kb-abc123" |
| `list_categories` | List categories | "What categories are available in my library?" |
| `list_recent_notes` | Recent notes | "What are my 10 most recent notes?" |

## Testing

### Test 1: List Categories

Ask Claude:
> "What categories are available in my Legato library?"

Expected: Claude calls `list_categories` and returns your category list with counts.

### Test 2: Search

Ask Claude:
> "Search my Legato library for notes about projects or implementation ideas"

Expected: Claude calls `search_library` and returns relevant notes with similarity scores.

### Test 3: Create Note

Ask Claude:
> "Create a new concept note in my Legato library titled 'Testing MCP Integration' with the content: 'This note was created via Claude MCP connector to verify the integration works correctly.'"

Expected: Claude calls `create_note`, creates the file in GitHub, and returns the entry_id.

### Test 4: Get Full Note

Ask Claude:
> "Show me the full content of my note about testing MCP"

Expected: Claude searches, finds the note, then calls `get_note` to fetch full content.

## Troubleshooting

### "Connection Failed"

1. Verify your Pit URL is HTTPS and publicly accessible
2. Check that `/.well-known/oauth-authorization-server` returns valid JSON:
   ```bash
   curl https://pit.legato.dev/.well-known/oauth-authorization-server
   ```

### "Authentication Failed"

1. Verify GitHub OAuth is configured (`GH_OAUTH_CLIENT_ID`, `GH_OAUTH_CLIENT_SECRET`)
2. Check your GitHub username is in `GH_ALLOWED_USERS`
3. Look at Pit server logs for OAuth errors

### "Invalid Token"

1. Token may have expired (1 hour lifetime)
2. Disconnect and reconnect the connector in Claude settings

### Tools Not Working

1. Verify Pit has `OPENAI_API_KEY` set (for semantic search embeddings)
2. Check Pit server logs for tool execution errors

## Manual API Testing

You can test the OAuth flow manually:

```bash
# 1. Check OAuth discovery
curl https://pit.legato.dev/.well-known/oauth-authorization-server

# 2. Test DCR (registration)
curl -X POST https://pit.legato.dev/oauth/register \
  -H "Content-Type: application/json" \
  -d '{"redirect_uris":["https://example.com/callback"],"client_name":"Test"}'

# 3. Test MCP protocol version
curl -I https://pit.legato.dev/mcp
# Should return: MCP-Protocol-Version: 2025-06-18

# 4. Test MCP endpoint (requires valid token)
curl -X POST https://pit.legato.dev/mcp \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

## Security Notes

- **Tokens are short-lived**: Access tokens expire after 1 hour
- **PKCE is enforced**: All OAuth flows use S256 code challenges
- **User allowlist**: Only users in `GH_ALLOWED_USERS` can authenticate
- **No GitHub token exposure**: Claude only receives Pit-issued JWTs, never your GitHub token

## Environment Variables

Ensure these are set on your Pit deployment:

| Variable | Required | Description |
|----------|----------|-------------|
| `GH_OAUTH_CLIENT_ID` | Yes | GitHub OAuth App Client ID |
| `GH_OAUTH_CLIENT_SECRET` | Yes | GitHub OAuth App Client Secret |
| `GH_ALLOWED_USERS` | Yes | Comma-separated allowed usernames |
| `FLASK_SECRET_KEY` | Yes | Secret for session/JWT signing |
| `OPENAI_API_KEY` | Recommended | For semantic search embeddings |
| `SYSTEM_PAT` | Yes | GitHub PAT for Library access |
