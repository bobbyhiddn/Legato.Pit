"""
MCP Protocol Handler for Claude.ai

Implements the Model Context Protocol (JSON-RPC 2.0) to expose
Legato Library tools and resources to Claude via the MCP connector.

Protocol version: 2025-06-18
"""

import os
import json
import hashlib
import re
import logging
from datetime import datetime
from functools import wraps

from flask import Blueprint, request, jsonify, g, current_app

from .oauth_server import require_mcp_auth, verify_access_token

logger = logging.getLogger(__name__)

mcp_bp = Blueprint('mcp', __name__, url_prefix='/mcp')

# MCP Protocol version (as of June 2025 spec)
MCP_PROTOCOL_VERSION = "2025-06-18"


def get_db():
    """Get legato database connection."""
    if 'mcp_db_conn' not in g:
        from .rag.database import init_db
        g.mcp_db_conn = init_db()
    return g.mcp_db_conn


def get_embedding_service():
    """Get embedding service for semantic search."""
    if 'mcp_embedding_service' not in g:
        from .rag.embedding_service import EmbeddingService
        from .rag.openai_provider import OpenAIEmbeddingProvider

        if not os.environ.get('OPENAI_API_KEY'):
            return None

        try:
            provider = OpenAIEmbeddingProvider()
            g.mcp_embedding_service = EmbeddingService(provider, get_db())
        except Exception as e:
            logger.warning(f"Could not create embedding service: {e}")
            return None

    return g.mcp_embedding_service


# ============ Protocol Version Discovery ============

@mcp_bp.route('', methods=['HEAD'])
def mcp_head():
    """Protocol version discovery.

    Claude checks this to verify server compatibility.
    """
    return '', 200, {
        'MCP-Protocol-Version': MCP_PROTOCOL_VERSION,
        'Content-Type': 'application/json'
    }


# ============ Main JSON-RPC Handler ============

@mcp_bp.route('', methods=['POST'])
@require_mcp_auth
def mcp_post():
    """Handle MCP JSON-RPC 2.0 requests.

    All MCP communication goes through this endpoint.
    Requests are routed to specific handlers based on the method.
    """
    try:
        msg = request.get_json()
    except Exception as e:
        return jsonify({
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": "Parse error"}
        }), 400

    if not msg:
        return jsonify({
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"}
        }), 400

    method = msg.get('method')
    params = msg.get('params', {})
    msg_id = msg.get('id')

    logger.debug(f"MCP request: method={method}")

    try:
        result = dispatch_mcp_method(method, params)
        return jsonify({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": result
        })
    except MCPError as e:
        return jsonify({
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": e.code, "message": e.message}
        })
    except Exception as e:
        logger.error(f"MCP handler error: {e}", exc_info=True)
        return jsonify({
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32603, "message": "Internal error"}
        }), 500


class MCPError(Exception):
    """MCP protocol error."""
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


# ============ Method Dispatcher ============

def dispatch_mcp_method(method: str, params: dict) -> dict:
    """Dispatch JSON-RPC method to handler."""

    handlers = {
        'initialize': handle_initialize,
        'initialized': handle_initialized,
        'ping': handle_ping,
        'tools/list': handle_tools_list,
        'tools/call': handle_tool_call,
        'resources/list': handle_resources_list,
        'resources/read': handle_resource_read,
        'prompts/list': handle_prompts_list,
        'prompts/get': handle_prompt_get,
    }

    handler = handlers.get(method)
    if not handler:
        raise MCPError(-32601, f"Method not found: {method}")

    return handler(params)


# ============ Lifecycle Handlers ============

def handle_initialize(params: dict) -> dict:
    """Handle initialize request - negotiate capabilities."""
    return {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {"listChanged": False},
            "prompts": {"listChanged": False}
        },
        "serverInfo": {
            "name": "legato-pit",
            "version": "1.0.0"
        }
    }


def handle_initialized(params: dict) -> dict:
    """Handle initialized notification - client is ready."""
    logger.info(f"MCP client initialized: {g.mcp_user.get('sub', 'unknown')}")
    return {}


def handle_ping(params: dict) -> dict:
    """Handle ping request."""
    return {"pong": True}


# ============ Tool Definitions ============

TOOLS = [
    {
        "name": "search_library",
        "description": "Hybrid search across Legato library notes using AI embeddings AND keyword matching. Returns results in two buckets: high-confidence matches and 'maybe related' lower-confidence matches.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query - describe what you're looking for"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results per bucket (default: 10)",
                    "default": 10
                },
                "category": {
                    "type": "string",
                    "description": "Optional: filter to a specific category (e.g., 'concept', 'epiphany')"
                },
                "expand_query": {
                    "type": "boolean",
                    "description": "Whether to expand query with synonyms/related terms for better recall (default: true)",
                    "default": True
                },
                "include_low_confidence": {
                    "type": "boolean",
                    "description": "Whether to include 'maybe related' lower-confidence results (default: true)",
                    "default": True
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "create_note",
        "description": "Create a new note in the Legato library. The note will be saved to GitHub and indexed for search.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "The title of the note"
                },
                "content": {
                    "type": "string",
                    "description": "The content of the note in markdown format"
                },
                "category": {
                    "type": "string",
                    "description": "The category for the note (e.g., 'concept', 'epiphany', 'reflection', 'glimmer', 'reminder', 'worklog')"
                }
            },
            "required": ["title", "content", "category"]
        }
    },
    {
        "name": "list_categories",
        "description": "List all available note categories in the Legato library.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "get_note",
        "description": "Get the full content of a specific note by its entry ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entry_id": {
                    "type": "string",
                    "description": "The entry ID (e.g., 'kb-abc12345')"
                }
            },
            "required": ["entry_id"]
        }
    },
    {
        "name": "list_recent_notes",
        "description": "List the most recently created notes in the library.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of notes to return (default: 20)",
                    "default": 20
                },
                "category": {
                    "type": "string",
                    "description": "Optional: filter to a specific category"
                }
            },
            "required": []
        }
    },
    {
        "name": "spawn_agent",
        "description": "Queue a chord project for human approval. Links 1-5 existing library notes to create a project that will be implemented by GitHub Copilot after approval. The project appears in the Legato agent queue for review.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "note_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 5,
                    "description": "Array of 1-5 entry_ids (e.g., ['kb-abc123']) to link to this project"
                },
                "project_name": {
                    "type": "string",
                    "description": "Slug-style name for the project (e.g., 'mcp-bedrock-adapter'). Auto-generated if not provided."
                },
                "project_type": {
                    "type": "string",
                    "enum": ["note", "chord"],
                    "description": "Project scope: 'note' for single-PR simple projects, 'chord' for complex multi-phase projects (default: 'note')",
                    "default": "note"
                },
                "additional_comments": {
                    "type": "string",
                    "description": "Additional context, instructions, or requirements for the implementation"
                }
            },
            "required": ["note_ids"]
        }
    }
]


def handle_tools_list(params: dict) -> dict:
    """Return list of available tools."""
    return {"tools": TOOLS}


def handle_tool_call(params: dict) -> dict:
    """Handle tool invocation."""
    name = params.get('name')
    args = params.get('arguments', {})

    tool_handlers = {
        'search_library': tool_search_library,
        'create_note': tool_create_note,
        'list_categories': tool_list_categories,
        'get_note': tool_get_note,
        'list_recent_notes': tool_list_recent_notes,
        'spawn_agent': tool_spawn_agent,
    }

    handler = tool_handlers.get(name)
    if not handler:
        raise MCPError(-32602, f"Unknown tool: {name}")

    try:
        result = handler(args)
        return {
            "content": [
                {"type": "text", "text": json.dumps(result, indent=2, default=str)}
            ]
        }
    except Exception as e:
        logger.error(f"Tool {name} failed: {e}", exc_info=True)
        return {
            "content": [
                {"type": "text", "text": f"Error: {str(e)}"}
            ],
            "isError": True
        }


# ============ Tool Implementations ============

def tool_search_library(args: dict) -> dict:
    """Hybrid search across library notes with optional query expansion."""
    query = args.get('query', '')
    limit = args.get('limit', 10)
    category = args.get('category')
    expand_query = args.get('expand_query', True)
    include_low_confidence = args.get('include_low_confidence', True)

    if not query:
        return {"error": "Query is required", "results": []}

    service = get_embedding_service()

    def format_result(r: dict) -> dict:
        """Format a result for output."""
        return {
            "entry_id": r['entry_id'],
            "title": r['title'],
            "category": r.get('category'),
            "similarity": round(r.get('similarity', 0), 3),
            "match_types": r.get('match_types', []),
            "snippet": (r.get('content', '')[:300] + '...') if r.get('content') else None
        }

    if service:
        # Use hybrid search with query expansion
        if expand_query:
            search_result = service.search_with_expansion(
                query=query,
                entry_type='knowledge',
                limit=limit,
                expand=True,
            )
        else:
            search_result = service.hybrid_search(
                query=query,
                entry_type='knowledge',
                limit=limit,
                include_low_confidence=include_low_confidence,
            )

        results = search_result.get('results', [])
        maybe_related = search_result.get('maybe_related', [])

        # Filter by category if specified
        if category:
            results = [r for r in results if r.get('category') == category]
            maybe_related = [r for r in maybe_related if r.get('category') == category]

        response = {
            "query": query,
            "results": [format_result(r) for r in results[:limit]],
            "search_type": "hybrid",
            "total_found": search_result.get('total_found', len(results)),
        }

        # Add query expansion info if used
        if expand_query and 'queries_used' in search_result:
            response["queries_used"] = search_result['queries_used']

        # Add maybe_related bucket if requested and has results
        if include_low_confidence and maybe_related:
            response["maybe_related"] = [format_result(r) for r in maybe_related[:limit]]

        return response

    else:
        # Fallback to text search
        db = get_db()
        sql = """
            SELECT entry_id, title, category, content
            FROM knowledge_entries
            WHERE title LIKE ? OR content LIKE ?
        """
        params = [f'%{query}%', f'%{query}%']

        if category:
            sql += " AND category = ?"
            params.append(category)

        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        results = db.execute(sql, params).fetchall()

        return {
            "query": query,
            "results": [
                {
                    "entry_id": r['entry_id'],
                    "title": r['title'],
                    "category": r['category'],
                    "snippet": (r['content'][:300] + '...') if r['content'] else None
                }
                for r in results
            ],
            "search_type": "text"
        }


def tool_create_note(args: dict) -> dict:
    """Create a new note in the library."""
    from .rag.database import get_user_categories
    from .rag.github_service import create_file

    title = args.get('title', '').strip()
    content = args.get('content', '').strip()
    category = args.get('category', '').lower().strip()

    if not title:
        return {"error": "Title is required"}
    if not category:
        return {"error": "Category is required"}

    # Validate category
    db = get_db()
    categories = get_user_categories(db, 'default')
    valid_categories = {c['name'] for c in categories}
    category_folders = {c['name']: c['folder_name'] for c in categories}

    if category not in valid_categories:
        return {
            "error": f"Invalid category. Must be one of: {', '.join(sorted(valid_categories))}"
        }

    # Generate entry_id
    hash_input = f"{title}-{datetime.utcnow().isoformat()}"
    entry_id = f"kb-{hashlib.sha256(hash_input.encode()).hexdigest()[:8]}"

    # Generate slug from title
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower())[:50].strip('-')
    if not slug:
        slug = entry_id

    # Build file path
    date_str = datetime.utcnow().strftime('%Y-%m-%d')
    folder = category_folders.get(category, f'{category}s')
    file_path = f'{folder}/{date_str}-{slug}.md'

    # Build frontmatter
    timestamp = datetime.utcnow().isoformat() + 'Z'
    frontmatter = f"""---
id: library.{category}.{slug}
title: "{title}"
category: {category}
created: {timestamp}
source: mcp-claude
domain_tags: []
key_phrases: []
---

"""
    full_content = frontmatter + content

    # Create file in GitHub
    token = current_app.config.get('SYSTEM_PAT')
    repo = 'bobbyhiddn/Legato.Library'

    create_file(
        repo=repo,
        path=file_path,
        content=full_content,
        message=f'Create note via MCP: {title}',
        token=token
    )

    # Insert into local database
    db.execute(
        """
        INSERT INTO knowledge_entries
        (entry_id, title, category, content, file_path, source_transcript, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'mcp-claude', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (entry_id, title, category, content, file_path)
    )
    db.commit()

    logger.info(f"MCP created note: {entry_id} - {title}")

    return {
        "success": True,
        "entry_id": entry_id,
        "title": title,
        "category": category,
        "file_path": file_path
    }


def tool_list_categories(args: dict) -> dict:
    """List all available categories."""
    from .rag.database import get_user_categories

    db = get_db()
    categories = get_user_categories(db, 'default')

    # Get counts per category
    counts = db.execute("""
        SELECT category, COUNT(*) as count
        FROM knowledge_entries
        GROUP BY category
    """).fetchall()
    count_map = {r['category']: r['count'] for r in counts}

    return {
        "categories": [
            {
                "name": c['name'],
                "display_name": c['display_name'],
                "description": c.get('description'),
                "note_count": count_map.get(c['name'], 0)
            }
            for c in categories
        ]
    }


def tool_get_note(args: dict) -> dict:
    """Get full content of a specific note."""
    entry_id = args.get('entry_id', '').strip()

    if not entry_id:
        return {"error": "entry_id is required"}

    db = get_db()
    entry = db.execute(
        """
        SELECT entry_id, title, category, content, file_path,
               created_at, updated_at, chord_status, chord_repo
        FROM knowledge_entries
        WHERE entry_id = ?
        """,
        (entry_id,)
    ).fetchone()

    if not entry:
        return {"error": f"Note not found: {entry_id}"}

    return {
        "entry_id": entry['entry_id'],
        "title": entry['title'],
        "category": entry['category'],
        "content": entry['content'],
        "file_path": entry['file_path'],
        "created_at": entry['created_at'],
        "updated_at": entry['updated_at'],
        "chord_status": entry['chord_status'],
        "chord_repo": entry['chord_repo']
    }


def tool_list_recent_notes(args: dict) -> dict:
    """List recently created notes."""
    limit = min(args.get('limit', 20), 100)  # Cap at 100
    category = args.get('category')

    db = get_db()

    if category:
        notes = db.execute(
            """
            SELECT entry_id, title, category, created_at
            FROM knowledge_entries
            WHERE category = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (category, limit)
        ).fetchall()
    else:
        notes = db.execute(
            """
            SELECT entry_id, title, category, created_at
            FROM knowledge_entries
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,)
        ).fetchall()

    return {
        "notes": [
            {
                "entry_id": n['entry_id'],
                "title": n['title'],
                "category": n['category'],
                "created_at": n['created_at']
            }
            for n in notes
        ],
        "count": len(notes)
    }


def tool_spawn_agent(args: dict) -> dict:
    """Queue a chord project from library notes for human approval."""
    import secrets
    import re
    from .rag.database import get_db_path, get_connection

    note_ids = args.get('note_ids', [])
    project_name = args.get('project_name', '').strip()
    project_type = args.get('project_type', 'note').lower()
    additional_comments = args.get('additional_comments', '').strip()

    # Validate note_ids
    if not note_ids:
        return {"error": "At least one note_id is required"}
    if len(note_ids) > 5:
        return {"error": "Maximum 5 notes can be linked to a project"}
    if not isinstance(note_ids, list):
        note_ids = [note_ids]

    # Validate project_type
    if project_type not in ('note', 'chord'):
        project_type = 'note'

    # Look up all the notes
    db = get_db()
    notes = []
    for nid in note_ids:
        entry = db.execute(
            "SELECT entry_id, title, category, content, domain_tags, key_phrases FROM knowledge_entries WHERE entry_id = ?",
            (nid.strip(),)
        ).fetchone()
        if entry:
            notes.append(dict(entry))
        else:
            return {"error": f"Note not found: {nid}"}

    if not notes:
        return {"error": "No valid notes found"}

    # Use first note as primary
    primary = notes[0]

    # Generate project name if not provided
    if not project_name:
        # Create slug from first note's title
        slug = re.sub(r'[^a-z0-9]+', '-', primary['title'].lower()).strip('-')
        project_name = slug[:50]  # Limit length

    # Generate queue_id
    queue_id = f"aq-{secrets.token_hex(6)}"

    # Build signal JSON
    signal_json = {
        "title": primary['title'],
        "intent": primary['content'][:500] if primary['content'] else "",
        "domain_tags": primary.get('domain_tags', '').split(',') if primary.get('domain_tags') else [],
        "source_notes": [n['entry_id'] for n in notes],
        "additional_comments": additional_comments,
    }

    # Build tasker body
    notes_section = "\n".join([f"- **{n['title']}** (`{n['entry_id']}`)" for n in notes])
    tasker_body = f"""## Tasker: {primary['title']}

### Linked Notes
{notes_section}

### Context
{primary['content'][:1000] if primary['content'] else 'No content'}

### Additional Comments
{additional_comments if additional_comments else 'None provided'}

---
*Queued via MCP by Claude | {len(notes)} note(s) linked*
"""

    # Build description
    if len(notes) > 1:
        description = f"Multi-note chord linking {len(notes)} notes: {', '.join(n['title'][:30] for n in notes)}"
    else:
        description = primary['content'][:200] if primary['content'] else primary['title']

    # Insert into agent_queue
    try:
        agents_db = get_connection(get_db_path("agents.db"))

        agents_db.execute(
            """
            INSERT INTO agent_queue
            (queue_id, project_name, project_type, title, description,
             signal_json, tasker_body, source_transcript, related_entry_id, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                queue_id,
                project_name,
                project_type,
                primary['title'],
                description,
                json.dumps(signal_json),
                tasker_body,
                'mcp-claude',
                ','.join(n['entry_id'] for n in notes),
            )
        )
        agents_db.commit()

        logger.info(f"MCP queued agent: {queue_id} - {project_name} ({len(notes)} notes)")

        return {
            "success": True,
            "queue_id": queue_id,
            "project_name": project_name,
            "project_type": project_type,
            "notes_linked": len(notes),
            "note_ids": [n['entry_id'] for n in notes],
            "message": f"Project '{project_name}' queued for approval. Visit /agents in Legato Pit to approve."
        }

    except Exception as e:
        logger.error(f"Failed to queue agent: {e}")
        return {"error": f"Failed to queue project: {str(e)}"}


# ============ Resource Handlers ============

RESOURCES = [
    {
        "uri": "legato://library/stats",
        "name": "Library Statistics",
        "description": "Overview of the Legato library - note counts, categories, etc.",
        "mimeType": "application/json"
    }
]


def handle_resources_list(params: dict) -> dict:
    """Return list of available resources."""
    return {"resources": RESOURCES}


def handle_resource_read(params: dict) -> dict:
    """Read a specific resource."""
    uri = params.get('uri', '')

    if uri == 'legato://library/stats':
        db = get_db()

        # Get total count
        total = db.execute("SELECT COUNT(*) FROM knowledge_entries").fetchone()[0]

        # Get category counts
        categories = db.execute("""
            SELECT category, COUNT(*) as count
            FROM knowledge_entries
            GROUP BY category
            ORDER BY count DESC
        """).fetchall()

        # Get recent activity
        recent = db.execute("""
            SELECT DATE(created_at) as date, COUNT(*) as count
            FROM knowledge_entries
            GROUP BY DATE(created_at)
            ORDER BY date DESC
            LIMIT 7
        """).fetchall()

        content = json.dumps({
            "total_notes": total,
            "categories": [{"name": c['category'], "count": c['count']} for c in categories],
            "recent_activity": [{"date": r['date'], "count": r['count']} for r in recent]
        }, indent=2)

        return {
            "contents": [
                {"uri": uri, "mimeType": "application/json", "text": content}
            ]
        }

    raise MCPError(-32602, f"Unknown resource: {uri}")


# ============ Prompt Handlers ============

PROMPTS = [
    {
        "name": "summarize_notes",
        "description": "Summarize notes from a category or search results",
        "arguments": [
            {
                "name": "category",
                "description": "Category to summarize",
                "required": False
            },
            {
                "name": "query",
                "description": "Search query to find notes to summarize",
                "required": False
            }
        ]
    }
]


def handle_prompts_list(params: dict) -> dict:
    """Return list of available prompts."""
    return {"prompts": PROMPTS}


def handle_prompt_get(params: dict) -> dict:
    """Get a specific prompt template."""
    name = params.get('name')
    arguments = params.get('arguments', {})

    if name == 'summarize_notes':
        category = arguments.get('category')
        query = arguments.get('query')

        if category:
            context = f"Summarize all notes in the '{category}' category."
        elif query:
            context = f"Search for notes about '{query}' and summarize the key insights."
        else:
            context = "Summarize the recent notes in the library."

        return {
            "messages": [
                {
                    "role": "user",
                    "content": {
                        "type": "text",
                        "text": f"{context}\n\nUse the search_library and get_note tools to find and read the notes, then provide a comprehensive summary."
                    }
                }
            ]
        }

    raise MCPError(-32602, f"Unknown prompt: {name}")
