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

# Disable strict slashes so /mcp and /mcp/ both work
mcp_bp.strict_slashes = False

# MCP Protocol version (as of June 2025 spec)
MCP_PROTOCOL_VERSION = "2025-06-18"


def get_db():
    """Get legato database connection for current user."""
    from .rag.database import get_user_legato_db
    return get_user_legato_db()


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

@mcp_bp.route('', methods=['HEAD', 'OPTIONS'])
@mcp_bp.route('/', methods=['HEAD', 'OPTIONS'])
def mcp_head():
    """Protocol version discovery and CORS preflight.

    Claude/ChatGPT checks this to verify server compatibility.
    """
    if request.method == 'OPTIONS':
        # CORS preflight
        response = current_app.make_default_options_response()
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, HEAD, OPTIONS'
        response.headers['MCP-Protocol-Version'] = MCP_PROTOCOL_VERSION
        return response

    return '', 200, {
        'MCP-Protocol-Version': MCP_PROTOCOL_VERSION,
        'Content-Type': 'application/json'
    }


# ============ Main JSON-RPC Handler ============

@mcp_bp.route('', methods=['POST'])
@mcp_bp.route('/', methods=['POST'])
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
        "description": "Create a new note in the Legato library. The note will be saved to GitHub and indexed for search. To create a task, include task_status (pending/in_progress/blocked/done) and optionally due_date.",
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
                },
                "task_status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "blocked", "done"],
                    "description": "Optional: Mark this note as a task with the given status. Tasks appear in the tasks view."
                },
                "due_date": {
                    "type": "string",
                    "description": "Optional: Due date for tasks in ISO format (YYYY-MM-DD)"
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
        "description": "Get the full content of a specific note. Supports lookup by entry_id (most reliable), file_path (stable), or title (fuzzy match). At least one lookup param required. If multiple provided, uses fallback chain: entry_id → file_path → title.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entry_id": {
                    "type": "string",
                    "description": "The entry ID (e.g., 'kb-abc12345') - most reliable lookup"
                },
                "file_path": {
                    "type": "string",
                    "description": "The file path in the library (e.g., 'concepts/2026-01-10-my-note.md') - stable identifier"
                },
                "title": {
                    "type": "string",
                    "description": "Note title for fuzzy matching - least reliable but convenient"
                }
            },
            "required": []
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
                },
                "target_chord_repo": {
                    "type": "string",
                    "description": "Optional: Target an existing chord repository (e.g., 'org/repo-name.Chord') instead of creating a new one. When provided, creates an incident issue on that repo for Copilot to work."
                }
            },
            "required": ["note_ids"]
        }
    },
    {
        "name": "update_note",
        "description": "Update an existing note in the Legato library. Updates both GitHub and local database.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entry_id": {
                    "type": "string",
                    "description": "The entry ID of the note to update (e.g., 'kb-abc12345')"
                },
                "title": {
                    "type": "string",
                    "description": "New title for the note (optional)"
                },
                "content": {
                    "type": "string",
                    "description": "New content for the note in markdown (optional)"
                },
                "category": {
                    "type": "string",
                    "description": "New category for the note (optional)"
                }
            },
            "required": ["entry_id"]
        }
    },
    {
        "name": "delete_note",
        "description": "Delete a note from the Legato library. Removes from both GitHub and local database. Requires confirmation flag.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entry_id": {
                    "type": "string",
                    "description": "The entry ID of the note to delete"
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true to confirm deletion. This is a safety check."
                }
            },
            "required": ["entry_id", "confirm"]
        }
    },
    {
        "name": "list_tasks",
        "description": "List notes that have been marked as tasks with their status. Filter by status, due date, or category.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "done", "blocked"],
                    "description": "Filter by task status"
                },
                "due_before": {
                    "type": "string",
                    "description": "Filter tasks due before this date (ISO format: YYYY-MM-DD)"
                },
                "due_after": {
                    "type": "string",
                    "description": "Filter tasks due after this date (ISO format: YYYY-MM-DD)"
                },
                "category": {
                    "type": "string",
                    "description": "Filter by note category"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of tasks to return (default: 50)",
                    "default": 50
                }
            },
            "required": []
        }
    },
    {
        "name": "update_task_status",
        "description": "Update or set the task status of a note. Use this to mark any existing note as a task, or to change the status of an existing task. Tasks appear in the dedicated tasks view.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entry_id": {
                    "type": "string",
                    "description": "The entry ID of the note to update (e.g., 'kb-abc12345')"
                },
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "done", "blocked"],
                    "description": "Task status: pending (not started), in_progress (active), blocked (waiting), done (completed)"
                },
                "due_date": {
                    "type": "string",
                    "description": "Optional due date in ISO format (YYYY-MM-DD)"
                }
            },
            "required": ["entry_id", "status"]
        }
    },
    {
        "name": "link_notes",
        "description": "Create an explicit relationship between two notes. Links are bidirectional for discovery.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_id": {
                    "type": "string",
                    "description": "Entry ID of the source note"
                },
                "target_id": {
                    "type": "string",
                    "description": "Entry ID of the target note"
                },
                "link_type": {
                    "type": "string",
                    "enum": ["related", "depends_on", "blocks", "implements", "references", "contradicts", "supports"],
                    "description": "Type of relationship (default: 'related')",
                    "default": "related"
                },
                "description": {
                    "type": "string",
                    "description": "Optional description of the relationship"
                }
            },
            "required": ["source_id", "target_id"]
        }
    },
    {
        "name": "get_note_context",
        "description": "Get a note with its full context: linked notes, semantic neighbors, and related projects.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entry_id": {
                    "type": "string",
                    "description": "The entry ID of the note"
                },
                "include_semantic": {
                    "type": "boolean",
                    "description": "Include semantically similar notes (default: true)",
                    "default": True
                },
                "semantic_limit": {
                    "type": "integer",
                    "description": "Max semantic neighbors to include (default: 5)",
                    "default": 5
                }
            },
            "required": ["entry_id"]
        }
    },
    {
        "name": "process_motif",
        "description": "Push text or markdown content into the transcript processing pipeline. Returns a job ID to check status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The text or markdown content to process"
                },
                "format": {
                    "type": "string",
                    "enum": ["markdown", "text", "transcript"],
                    "description": "Content format (default: 'markdown')",
                    "default": "markdown"
                },
                "source_label": {
                    "type": "string",
                    "description": "Label for the source of this content (e.g., 'claude-conversation', 'external-doc')"
                }
            },
            "required": ["content"]
        }
    },
    {
        "name": "get_processing_status",
        "description": "Check the status of an async processing job.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The job ID returned from process_motif"
                }
            },
            "required": ["job_id"]
        }
    },
    {
        "name": "check_connection",
        "description": "Diagnostic tool to check MCP connection status, user authentication, and GitHub App setup. Use this to troubleshoot connectivity issues.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
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
        'update_note': tool_update_note,
        'delete_note': tool_delete_note,
        'list_tasks': tool_list_tasks,
        'update_task_status': tool_update_task_status,
        'link_notes': tool_link_notes,
        'get_note_context': tool_get_note_context,
        'process_motif': tool_process_motif,
        'get_processing_status': tool_get_processing_status,
        'check_connection': tool_check_connection,
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


def compute_content_hash(content: str) -> str:
    """Compute a stable hash of content for deduplication and integrity."""
    normalized = content.strip()
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def generate_slug(title: str) -> str:
    """Generate a URL-safe slug from a title."""
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower())[:50].strip('-')
    return slug or 'untitled'


def generate_entry_id(category: str, title: str, content_hash: str = None) -> str:
    """Generate a canonical entry ID in the standard format.

    Args:
        category: Entry category (singular form like 'concept')
        title: Entry title
        content_hash: Optional content hash to append for disambiguation

    Returns:
        Entry ID like "library.concept.my-note-title" or
        "library.concept.my-note-title-abc123" if disambiguated
    """
    slug = generate_slug(title)
    base_id = f"library.{category}.{slug}"
    if content_hash:
        # Append first 6 chars of hash to disambiguate
        return f"{base_id}-{content_hash[:6]}"
    return base_id


def tool_create_note(args: dict) -> dict:
    """Create a new note in the library."""
    from .rag.database import get_user_categories
    from .rag.github_service import create_file

    title = args.get('title', '').strip()
    content = args.get('content', '').strip()
    category = args.get('category', '').lower().strip()
    task_status = args.get('task_status', '').strip() if args.get('task_status') else None
    due_date = args.get('due_date', '').strip() if args.get('due_date') else None

    if not title:
        return {"error": "Title is required"}
    if not category:
        return {"error": "Category is required"}

    # Validate task_status if provided
    valid_statuses = {'pending', 'in_progress', 'blocked', 'done'}
    if task_status and task_status not in valid_statuses:
        return {"error": f"Invalid task_status. Must be one of: {', '.join(sorted(valid_statuses))}"}

    # Validate category
    db = get_db()
    categories = get_user_categories(db, 'default')
    valid_categories = {c['name'] for c in categories}
    category_folders = {c['name']: c['folder_name'] for c in categories}

    if category not in valid_categories:
        return {
            "error": f"Invalid category. Must be one of: {', '.join(sorted(valid_categories))}"
        }

    # Compute content hash for integrity/deduplication
    content_hash = compute_content_hash(content)

    # Generate slug and canonical entry_id
    slug = generate_slug(title)

    # Canonical ID format: library.{category}.{slug}
    # This matches what library_sync expects from frontmatter
    entry_id = generate_entry_id(category, title)

    # Check for entry_id collision with existing entry
    # This handles long titles that truncate to the same slug
    collision = db.execute(
        "SELECT entry_id FROM knowledge_entries WHERE entry_id = ?",
        (entry_id,)
    ).fetchone()
    if collision:
        logger.info(f"Entry ID collision detected for '{title}', disambiguating with content hash")
        entry_id = generate_entry_id(category, title, content_hash)

    # Build file path
    date_str = datetime.utcnow().strftime('%Y-%m-%d')
    folder = category_folders.get(category, f'{category}s')
    file_path = f'{folder}/{date_str}-{slug}.md'

    # Build frontmatter - include task fields if provided
    timestamp = datetime.utcnow().isoformat() + 'Z'
    frontmatter_lines = [
        '---',
        f'id: {entry_id}',
        f'title: "{title}"',
        f'category: {category}',
        f'created: {timestamp}',
        f'content_hash: {content_hash}',
        'source: mcp-claude',
        'domain_tags: []',
        'key_phrases: []',
    ]

    # Add task fields to frontmatter if this is a task
    if task_status:
        frontmatter_lines.append(f'task_status: {task_status}')
    if due_date:
        frontmatter_lines.append(f'due_date: {due_date}')

    frontmatter_lines.append('---')
    frontmatter_lines.append('')
    frontmatter = '\n'.join(frontmatter_lines)

    full_content = frontmatter + content

    # Create file in GitHub using user's installation token
    from .auth import get_user_installation_token
    from .core import get_user_library_repo

    user_id = g.mcp_user.get('user_id') if hasattr(g, 'mcp_user') else None
    github_login = g.mcp_user.get('sub') if hasattr(g, 'mcp_user') else None
    logger.info(f"MCP create_note: user_id={user_id}, github_login={github_login}")

    token = get_user_installation_token(user_id, 'library') if user_id else None
    if not token:
        logger.warning(f"MCP create_note: No token for user_id={user_id} - user may need to complete GitHub App setup via web")
        return {"error": "GitHub authorization required. Please re-authenticate."}

    repo = get_user_library_repo(user_id)

    create_file(
        repo=repo,
        path=file_path,
        content=full_content,
        message=f'Create note via MCP: {title}',
        token=token
    )

    # Insert into local database with task fields and content_hash
    if task_status:
        db.execute(
            """
            INSERT INTO knowledge_entries
            (entry_id, title, category, content, file_path, source_transcript, task_status, due_date, content_hash, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'mcp-claude', ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (entry_id, title, category, content, file_path, task_status, due_date, content_hash)
        )
    else:
        db.execute(
            """
            INSERT INTO knowledge_entries
            (entry_id, title, category, content, file_path, source_transcript, content_hash, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'mcp-claude', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (entry_id, title, category, content, file_path, content_hash)
        )
    db.commit()

    logger.info(f"MCP created note: {entry_id} - {title}" + (f" [task:{task_status}]" if task_status else ""))

    result = {
        "success": True,
        "entry_id": entry_id,
        "title": title,
        "category": category,
        "file_path": file_path,
        "available_categories": sorted(valid_categories)
    }

    # Include task fields in response if set
    if task_status:
        result["task_status"] = task_status
    if due_date:
        result["due_date"] = due_date

    return result


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
    """Get full content of a specific note with multi-method lookup."""
    entry_id = args.get('entry_id', '').strip() if args.get('entry_id') else None
    file_path = args.get('file_path', '').strip() if args.get('file_path') else None
    title = args.get('title', '').strip() if args.get('title') else None

    if not entry_id and not file_path and not title:
        return {"error": "At least one lookup parameter required: entry_id, file_path, or title"}

    db = get_db()
    entry = None
    lookup_method = None

    # Fallback chain: entry_id → file_path → title
    if entry_id:
        entry = db.execute(
            """
            SELECT entry_id, title, category, content, file_path,
                   created_at, updated_at, chord_status, chord_repo, task_status, due_date
            FROM knowledge_entries
            WHERE entry_id = ?
            """,
            (entry_id,)
        ).fetchone()
        lookup_method = "entry_id"

    if not entry and file_path:
        entry = db.execute(
            """
            SELECT entry_id, title, category, content, file_path,
                   created_at, updated_at, chord_status, chord_repo, task_status, due_date
            FROM knowledge_entries
            WHERE file_path = ?
            """,
            (file_path,)
        ).fetchone()
        lookup_method = "file_path"

    if not entry and title:
        # Fuzzy match: case-insensitive LIKE search
        entry = db.execute(
            """
            SELECT entry_id, title, category, content, file_path,
                   created_at, updated_at, chord_status, chord_repo, task_status, due_date
            FROM knowledge_entries
            WHERE LOWER(title) LIKE LOWER(?)
            ORDER BY
                CASE WHEN LOWER(title) = LOWER(?) THEN 0 ELSE 1 END,
                updated_at DESC
            LIMIT 1
            """,
            (f'%{title}%', title)
        ).fetchone()
        lookup_method = "title"

    if not entry:
        search_term = entry_id or file_path or title
        return {"error": f"Note not found: {search_term}"}

    return {
        "entry_id": entry['entry_id'],
        "title": entry['title'],
        "category": entry['category'],
        "content": entry['content'],
        "file_path": entry['file_path'],
        "created_at": entry['created_at'],
        "updated_at": entry['updated_at'],
        "chord_status": entry['chord_status'],
        "chord_repo": entry['chord_repo'],
        "task_status": entry['task_status'],
        "due_date": entry['due_date'],
        "lookup_method": lookup_method
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


def _create_incident_on_chord(chord_repo: str, notes: list, additional_comments: str, user_id: str = None, github_login: str = None) -> dict:
    """Dispatch an incident to Conduct for an existing chord repository.

    Args:
        chord_repo: Full repo name (org/repo-name.Chord)
        notes: List of note dicts with entry_id, title, content
        additional_comments: Additional context for the incident
        user_id: User ID for multi-tenant mode
        github_login: GitHub username (org) for dispatch

    Returns:
        dict with success/error and dispatch details
    """
    import os
    import secrets
    import requests as http_requests
    from .auth import get_user_installation_token

    # Get user token in multi-tenant mode
    token = get_user_installation_token(user_id, 'library') if user_id else None
    if not token:
        return {"error": "GitHub authorization required. Please re-authenticate."}

    # Use user's GitHub login as org, fallback to env var
    org = github_login or os.environ.get('LEGATO_ORG', 'bobbyhiddn')
    conduct_repo = os.environ.get('CONDUCT_REPO', 'Legato.Conduct')

    primary = notes[0]

    # Build issue title (Conduct will use this)
    issue_title = primary['title']

    # Build tasker body for the incident
    notes_section = "\n".join([f"- **{n['title']}** (`{n['entry_id']}`)" for n in notes])
    tasker_body = f"""## Incident: {primary['title']}

### Linked Notes
{notes_section}

### Context
{primary['content'][:1500] if primary['content'] else 'No content'}

### Additional Comments
{additional_comments if additional_comments else 'None provided'}

---
*Incident dispatched via MCP by Claude | {len(notes)} note(s) linked*
"""

    # Generate a queue_id for tracking
    queue_id = f"incident-{secrets.token_hex(6)}"

    # Dispatch to Conduct with target_repo to create incident on existing chord
    payload = {
        'event_type': 'spawn-agent',
        'client_payload': {
            'queue_id': queue_id,
            'target_repo': chord_repo,
            'issue_title': issue_title,
            'tasker_body': tasker_body,
        }
    }

    try:
        response = http_requests.post(
            f'https://api.github.com/repos/{org}/{conduct_repo}/dispatches',
            json=payload,
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28',
            },
            timeout=15,
        )

        # 204 No Content = success for repository_dispatch
        if response.status_code == 204:
            logger.info(f"Dispatched incident to Conduct for {chord_repo}: {queue_id}")

            return {
                "success": True,
                "incident_dispatched": True,
                "queue_id": queue_id,
                "chord_repo": chord_repo,
                "notes_linked": len(notes),
                "note_ids": [n['entry_id'] for n in notes],
                "message": f"Incident dispatched to Conduct for {chord_repo}. Copilot will work this issue."
            }
        else:
            logger.error(f"Dispatch failed: {response.status_code} - {response.text}")
            return {
                "error": f"Failed to dispatch incident: HTTP {response.status_code}",
                "detail": response.text
            }

    except Exception as e:
        logger.error(f"Failed to dispatch incident for {chord_repo}: {e}")
        return {"error": f"Failed to dispatch incident: {str(e)}"}


def tool_spawn_agent(args: dict) -> dict:
    """Queue a chord project from library notes for human approval, or create an incident on an existing chord."""
    import secrets
    import re
    import requests as http_requests
    from .rag.database import get_db_path, get_connection

    note_ids = args.get('note_ids', [])
    project_name = args.get('project_name', '').strip()
    project_type = args.get('project_type', 'note').lower()
    additional_comments = args.get('additional_comments', '').strip()
    target_chord_repo = args.get('target_chord_repo', '').strip()

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

    # Get user context from MCP auth (needed for both incident and queue flows)
    user_id = g.mcp_user.get('user_id') if hasattr(g, 'mcp_user') else None
    github_login = g.mcp_user.get('sub') if hasattr(g, 'mcp_user') else None

    # If targeting an existing chord, create an incident issue instead of queuing
    if target_chord_repo:
        return _create_incident_on_chord(target_chord_repo, notes, additional_comments, user_id, github_login)

    # Generate project name if not provided
    if not project_name:
        # Create slug from first note's title
        slug = re.sub(r'[^a-z0-9]+', '-', primary['title'].lower()).strip('-')
        project_name = slug[:50]  # Limit length

    # Generate queue_id
    queue_id = f"aq-{secrets.token_hex(6)}"

    # Build signal JSON
    repo_suffix = "Chord" if project_type == "chord" else "Note"
    signal_json = {
        "title": primary['title'],
        "intent": primary['content'][:500] if primary['content'] else "",
        "domain_tags": primary.get('domain_tags', '').split(',') if primary.get('domain_tags') else [],
        "source_notes": [n['entry_id'] for n in notes],
        "additional_comments": additional_comments,
        "path": f"{project_name}.{repo_suffix}",
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

    # Build initial comments array with Claude's comment if provided
    initial_comments = []
    if additional_comments:
        initial_comments.append({
            "text": additional_comments,
            "author": "claude",
            "timestamp": datetime.utcnow().isoformat() + "Z"
        })

    # Insert into agent_queue
    try:
        agents_db = get_connection(get_db_path("agents.db"))

        agents_db.execute(
            """
            INSERT INTO agent_queue
            (queue_id, project_name, project_type, title, description,
             signal_json, tasker_body, source_transcript, related_entry_id, comments, status, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
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
                json.dumps(initial_comments),
                user_id,  # Multi-tenant: isolate by user
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


def tool_update_note(args: dict) -> dict:
    """Update an existing note in both GitHub and local database."""
    from .rag.database import get_user_categories
    from .rag.github_service import commit_file, get_file_content

    entry_id = args.get('entry_id', '').strip()
    new_title = args.get('title', '').strip() if args.get('title') else None
    new_content = args.get('content', '').strip() if args.get('content') else None
    new_category = args.get('category', '').lower().strip() if args.get('category') else None

    if not entry_id:
        return {"error": "entry_id is required"}

    if not new_title and not new_content and not new_category:
        return {"error": "At least one of title, content, or category must be provided"}

    db = get_db()

    # Get existing note
    entry = db.execute(
        """
        SELECT entry_id, title, category, content, file_path
        FROM knowledge_entries
        WHERE entry_id = ?
        """,
        (entry_id,)
    ).fetchone()

    if not entry:
        return {"error": f"Note not found: {entry_id}"}

    # Get user context from MCP auth
    user_id = g.mcp_user.get('user_id') if hasattr(g, 'mcp_user') else None

    # Validate new category if provided
    if new_category:
        categories = get_user_categories(db, user_id or 'default')
        valid_categories = {c['name'] for c in categories}
        if new_category not in valid_categories:
            return {"error": f"Invalid category. Must be one of: {', '.join(sorted(valid_categories))}"}

    # Use existing values as defaults
    title = new_title or entry['title']
    content = new_content if new_content is not None else entry['content']
    category = new_category or entry['category']
    file_path = entry['file_path']

    # Recompute content_hash if content changed
    new_content_hash = compute_content_hash(content) if new_content is not None else None

    # Get user's installation token
    from .auth import get_user_installation_token
    from .core import get_user_library_repo

    token = get_user_installation_token(user_id, 'library') if user_id else None
    if not token:
        return {"error": "GitHub authorization required. Please re-authenticate."}

    repo = get_user_library_repo(user_id)

    try:
        current_content = get_file_content(repo, file_path, token)
        if current_content:
            # Parse existing frontmatter
            if current_content.startswith('---'):
                parts = current_content.split('---', 2)
                if len(parts) >= 3:
                    frontmatter_lines = parts[1].strip().split('\n')
                    # Update frontmatter fields
                    new_frontmatter_lines = []
                    has_content_hash = False
                    for line in frontmatter_lines:
                        if line.startswith('title:') and new_title:
                            new_frontmatter_lines.append(f'title: "{title}"')
                        elif line.startswith('category:') and new_category:
                            new_frontmatter_lines.append(f'category: {category}')
                        elif line.startswith('content_hash:') and new_content_hash:
                            new_frontmatter_lines.append(f'content_hash: {new_content_hash}')
                            has_content_hash = True
                        else:
                            new_frontmatter_lines.append(line)
                    # Add content_hash if it wasn't in frontmatter but content changed
                    if new_content_hash and not has_content_hash:
                        new_frontmatter_lines.append(f'content_hash: {new_content_hash}')
                    full_content = f"---\n{chr(10).join(new_frontmatter_lines)}\n---\n\n{content}"
                else:
                    full_content = content
            else:
                full_content = content
        else:
            # File doesn't exist in GitHub, build new frontmatter
            timestamp = datetime.utcnow().isoformat() + 'Z'
            slug = generate_slug(title)
            content_hash = compute_content_hash(content)
            full_content = f"""---
id: library.{category}.{slug}
title: "{title}"
category: {category}
created: {timestamp}
content_hash: {content_hash}
source: mcp-claude
domain_tags: []
key_phrases: []
---

{content}"""

        # Commit to GitHub
        commit_file(
            repo=repo,
            path=file_path,
            content=full_content,
            message=f'Update note via MCP: {title}',
            token=token
        )

        # Update local database (include content_hash if content changed)
        if new_content_hash:
            db.execute(
                """
                UPDATE knowledge_entries
                SET title = ?, category = ?, content = ?, content_hash = ?, updated_at = CURRENT_TIMESTAMP
                WHERE entry_id = ?
                """,
                (title, category, content, new_content_hash, entry_id)
            )
        else:
            db.execute(
                """
                UPDATE knowledge_entries
                SET title = ?, category = ?, content = ?, updated_at = CURRENT_TIMESTAMP
                WHERE entry_id = ?
                """,
                (title, category, content, entry_id)
            )
        db.commit()

        logger.info(f"MCP updated note: {entry_id} - {title}")

        return {
            "success": True,
            "entry_id": entry_id,
            "title": title,
            "category": category,
            "file_path": file_path,
            "changes": {
                "title": new_title is not None,
                "content": new_content is not None,
                "category": new_category is not None
            }
        }

    except Exception as e:
        logger.error(f"Failed to update note: {e}")
        return {"error": f"Failed to update note: {str(e)}"}


def tool_delete_note(args: dict) -> dict:
    """Delete a note from both GitHub and local database."""
    from .rag.github_service import delete_file

    entry_id = args.get('entry_id', '').strip()
    confirm = args.get('confirm', False)

    if not entry_id:
        return {"error": "entry_id is required"}

    if not confirm:
        return {
            "error": "Deletion requires confirmation. Set confirm=true to proceed.",
            "warning": "This will permanently delete the note from both GitHub and the local database."
        }

    db = get_db()

    # Get existing note
    entry = db.execute(
        """
        SELECT entry_id, title, file_path
        FROM knowledge_entries
        WHERE entry_id = ?
        """,
        (entry_id,)
    ).fetchone()

    if not entry:
        return {"error": f"Note not found: {entry_id}"}

    # Get user's installation token
    from .auth import get_user_installation_token
    from .core import get_user_library_repo

    user_id = g.mcp_user.get('user_id') if hasattr(g, 'mcp_user') else None
    token = get_user_installation_token(user_id, 'library') if user_id else None
    if not token:
        return {"error": "GitHub authorization required. Please re-authenticate."}

    repo = get_user_library_repo(user_id)
    file_path = entry['file_path']
    title = entry['title']

    try:
        # Delete from GitHub
        if file_path:
            try:
                delete_file(
                    repo=repo,
                    path=file_path,
                    message=f'Delete note via MCP: {title}',
                    token=token
                )
            except Exception as e:
                # File might not exist in GitHub, continue with local deletion
                logger.warning(f"Could not delete from GitHub (may not exist): {e}")

        # Delete from local database
        db.execute("DELETE FROM knowledge_entries WHERE entry_id = ?", (entry_id,))

        # Also delete any links involving this note
        db.execute("DELETE FROM note_links WHERE source_entry_id = ? OR target_entry_id = ?", (entry_id, entry_id))

        # Delete embeddings
        db.execute("DELETE FROM embeddings WHERE entry_id = (SELECT id FROM knowledge_entries WHERE entry_id = ?)", (entry_id,))

        db.commit()

        logger.info(f"MCP deleted note: {entry_id} - {title}")

        return {
            "success": True,
            "deleted": {
                "entry_id": entry_id,
                "title": title,
                "file_path": file_path
            }
        }

    except Exception as e:
        logger.error(f"Failed to delete note: {e}")
        return {"error": f"Failed to delete note: {str(e)}"}


def tool_list_tasks(args: dict) -> dict:
    """List notes marked as tasks with optional filtering."""
    status = args.get('status')
    due_before = args.get('due_before')
    due_after = args.get('due_after')
    category = args.get('category')
    limit = min(args.get('limit', 50), 100)

    db = get_db()

    # Build query dynamically
    sql = """
        SELECT entry_id, title, category, task_status, due_date, created_at, updated_at
        FROM knowledge_entries
        WHERE task_status IS NOT NULL
    """
    params = []

    if status:
        sql += " AND task_status = ?"
        params.append(status)

    if due_before:
        sql += " AND due_date <= ?"
        params.append(due_before)

    if due_after:
        sql += " AND due_date >= ?"
        params.append(due_after)

    if category:
        sql += " AND category = ?"
        params.append(category)

    sql += " ORDER BY CASE task_status WHEN 'blocked' THEN 0 WHEN 'in_progress' THEN 1 WHEN 'pending' THEN 2 ELSE 3 END, due_date ASC NULLS LAST, updated_at DESC LIMIT ?"
    params.append(limit)

    tasks = db.execute(sql, params).fetchall()

    # Get counts by status
    status_counts = db.execute("""
        SELECT task_status, COUNT(*) as count
        FROM knowledge_entries
        WHERE task_status IS NOT NULL
        GROUP BY task_status
    """).fetchall()

    return {
        "tasks": [
            {
                "entry_id": t['entry_id'],
                "title": t['title'],
                "category": t['category'],
                "status": t['task_status'],
                "due_date": t['due_date'],
                "created_at": t['created_at'],
                "updated_at": t['updated_at']
            }
            for t in tasks
        ],
        "count": len(tasks),
        "status_counts": {r['task_status']: r['count'] for r in status_counts}
    }


def tool_update_task_status(args: dict) -> dict:
    """Update task status for a note."""
    entry_id = args.get('entry_id', '').strip()
    status = args.get('status', '').strip()
    due_date = args.get('due_date', '').strip() if args.get('due_date') else None

    if not entry_id:
        return {"error": "entry_id is required"}

    if not status:
        return {"error": "status is required"}

    valid_statuses = {'pending', 'in_progress', 'done', 'blocked'}
    if status not in valid_statuses:
        return {"error": f"Invalid status. Must be one of: {', '.join(sorted(valid_statuses))}"}

    db = get_db()

    # Check note exists
    entry = db.execute(
        "SELECT entry_id, title, task_status FROM knowledge_entries WHERE entry_id = ?",
        (entry_id,)
    ).fetchone()

    if not entry:
        return {"error": f"Note not found: {entry_id}"}

    old_status = entry['task_status']

    # Update task status and optionally due_date
    if due_date:
        db.execute(
            """
            UPDATE knowledge_entries
            SET task_status = ?, due_date = ?, updated_at = CURRENT_TIMESTAMP
            WHERE entry_id = ?
            """,
            (status, due_date, entry_id)
        )
    else:
        db.execute(
            """
            UPDATE knowledge_entries
            SET task_status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE entry_id = ?
            """,
            (status, entry_id)
        )
    db.commit()

    logger.info(f"MCP updated task status: {entry_id} {old_status} -> {status}")

    return {
        "success": True,
        "entry_id": entry_id,
        "title": entry['title'],
        "old_status": old_status,
        "new_status": status,
        "due_date": due_date
    }


def tool_link_notes(args: dict) -> dict:
    """Create an explicit relationship between two notes."""
    source_id = args.get('source_id', '').strip()
    target_id = args.get('target_id', '').strip()
    link_type = args.get('link_type', 'related').strip()
    description = args.get('description', '').strip() if args.get('description') else None

    if not source_id or not target_id:
        return {"error": "Both source_id and target_id are required"}

    if source_id == target_id:
        return {"error": "Cannot link a note to itself"}

    valid_link_types = {'related', 'depends_on', 'blocks', 'implements', 'references', 'contradicts', 'supports'}
    if link_type not in valid_link_types:
        return {"error": f"Invalid link_type. Must be one of: {', '.join(sorted(valid_link_types))}"}

    db = get_db()

    # Verify both notes exist
    source = db.execute("SELECT entry_id, title FROM knowledge_entries WHERE entry_id = ?", (source_id,)).fetchone()
    target = db.execute("SELECT entry_id, title FROM knowledge_entries WHERE entry_id = ?", (target_id,)).fetchone()

    if not source:
        return {"error": f"Source note not found: {source_id}"}
    if not target:
        return {"error": f"Target note not found: {target_id}"}

    try:
        # Insert the link (ignore if already exists)
        db.execute(
            """
            INSERT OR IGNORE INTO note_links (source_entry_id, target_entry_id, link_type, description, created_by)
            VALUES (?, ?, ?, ?, 'mcp-claude')
            """,
            (source_id, target_id, link_type, description)
        )

        # For bidirectional discovery, also create reverse link for symmetric types
        symmetric_types = {'related', 'contradicts'}
        if link_type in symmetric_types:
            db.execute(
                """
                INSERT OR IGNORE INTO note_links (source_entry_id, target_entry_id, link_type, description, created_by)
                VALUES (?, ?, ?, ?, 'mcp-claude')
                """,
                (target_id, source_id, link_type, description)
            )

        db.commit()

        logger.info(f"MCP linked notes: {source_id} --[{link_type}]--> {target_id}")

        return {
            "success": True,
            "link": {
                "source": {"entry_id": source_id, "title": source['title']},
                "target": {"entry_id": target_id, "title": target['title']},
                "type": link_type,
                "description": description
            }
        }

    except Exception as e:
        logger.error(f"Failed to link notes: {e}")
        return {"error": f"Failed to create link: {str(e)}"}


def tool_get_note_context(args: dict) -> dict:
    """Get a note with its full context: linked notes and semantic neighbors."""
    entry_id = args.get('entry_id', '').strip()
    include_semantic = args.get('include_semantic', True)
    semantic_limit = min(args.get('semantic_limit', 5), 20)

    if not entry_id:
        return {"error": "entry_id is required"}

    db = get_db()

    # Get the main note
    entry = db.execute(
        """
        SELECT entry_id, title, category, content, file_path, task_status, due_date,
               created_at, updated_at, chord_status, chord_repo
        FROM knowledge_entries
        WHERE entry_id = ?
        """,
        (entry_id,)
    ).fetchone()

    if not entry:
        return {"error": f"Note not found: {entry_id}"}

    # Get outgoing links (this note links to others)
    outgoing = db.execute(
        """
        SELECT nl.target_entry_id, nl.link_type, nl.description,
               ke.title, ke.category
        FROM note_links nl
        JOIN knowledge_entries ke ON ke.entry_id = nl.target_entry_id
        WHERE nl.source_entry_id = ?
        """,
        (entry_id,)
    ).fetchall()

    # Get incoming links (others link to this note)
    incoming = db.execute(
        """
        SELECT nl.source_entry_id, nl.link_type, nl.description,
               ke.title, ke.category
        FROM note_links nl
        JOIN knowledge_entries ke ON ke.entry_id = nl.source_entry_id
        WHERE nl.target_entry_id = ?
        """,
        (entry_id,)
    ).fetchall()

    # Get semantic neighbors if requested
    semantic_neighbors = []
    if include_semantic:
        service = get_embedding_service()
        if service:
            try:
                # Search for similar notes
                search_result = service.hybrid_search(
                    query=entry['title'] + " " + (entry['content'][:500] if entry['content'] else ""),
                    entry_type='knowledge',
                    limit=semantic_limit + 1,  # +1 to exclude self
                    include_low_confidence=False,
                )
                for r in search_result.get('results', []):
                    if r['entry_id'] != entry_id:
                        semantic_neighbors.append({
                            "entry_id": r['entry_id'],
                            "title": r['title'],
                            "category": r.get('category'),
                            "similarity": round(r.get('similarity', 0), 3)
                        })
                        if len(semantic_neighbors) >= semantic_limit:
                            break
            except Exception as e:
                logger.warning(f"Could not get semantic neighbors: {e}")

    # Get related projects from agent queue (filtered by user for multi-tenant)
    from .rag.database import get_db_path, get_connection
    user_id = g.mcp_user.get('user_id') if hasattr(g, 'mcp_user') else None
    try:
        agents_db = get_connection(get_db_path("agents.db"))
        projects = agents_db.execute(
            """
            SELECT queue_id, project_name, project_type, status, title
            FROM agent_queue
            WHERE related_entry_id LIKE ? AND user_id = ?
            ORDER BY created_at DESC
            LIMIT 5
            """,
            (f'%{entry_id}%', user_id)
        ).fetchall()
    except Exception:
        projects = []

    return {
        "note": {
            "entry_id": entry['entry_id'],
            "title": entry['title'],
            "category": entry['category'],
            "content": entry['content'],
            "file_path": entry['file_path'],
            "task_status": entry['task_status'],
            "due_date": entry['due_date'],
            "created_at": entry['created_at'],
            "updated_at": entry['updated_at'],
            "chord_status": entry['chord_status'],
            "chord_repo": entry['chord_repo']
        },
        "links": {
            "outgoing": [
                {
                    "entry_id": l['target_entry_id'],
                    "title": l['title'],
                    "category": l['category'],
                    "link_type": l['link_type'],
                    "description": l['description']
                }
                for l in outgoing
            ],
            "incoming": [
                {
                    "entry_id": l['source_entry_id'],
                    "title": l['title'],
                    "category": l['category'],
                    "link_type": l['link_type'],
                    "description": l['description']
                }
                for l in incoming
            ]
        },
        "semantic_neighbors": semantic_neighbors,
        "related_projects": [
            {
                "queue_id": p['queue_id'],
                "project_name": p['project_name'],
                "project_type": p['project_type'],
                "status": p['status'],
                "title": p['title']
            }
            for p in projects
        ]
    }


def tool_process_motif(args: dict) -> dict:
    """Push content into the transcript processing pipeline.

    Uses the Pit-native MotifProcessor which:
    - Parses the transcript into threads
    - Classifies each thread using Claude
    - Correlates with existing entries
    - Extracts markdown artifacts
    - Writes to the user's Library
    """
    from flask import g
    from .motif_processor import process_motif_sync

    content = args.get('content', '').strip()
    source_label = args.get('source_label', 'mcp-direct')

    if not content:
        return {"error": "content is required"}

    if len(content) < 10:
        return {"error": "Content too short. Minimum 10 characters required."}

    # Get user_id from MCP context
    if not hasattr(g, 'mcp_user') or not g.mcp_user:
        return {"error": "Authentication required"}

    user_id = g.mcp_user.get('user_id')
    if not user_id:
        return {"error": "User ID not found in token"}

    try:
        # Use the new Pit-native motif processor
        # This processes synchronously using the user's own Anthropic API key
        result = process_motif_sync(content, user_id, source_label)

        if result.get('status') == 'completed':
            return {
                "success": True,
                "job_id": result.get('job_id'),
                "status": "completed",
                "result": {
                    "entry_ids": result.get('entry_ids', []),
                    "notes_created": len(result.get('entry_ids', []))
                }
            }
        elif result.get('status') == 'failed':
            return {
                "success": False,
                "job_id": result.get('job_id'),
                "status": "failed",
                "error": result.get('error', 'Processing failed')
            }
        else:
            # Pending/processing - should not happen in sync mode
            return {
                "success": True,
                "job_id": result.get('job_id'),
                "status": result.get('status', 'pending'),
                "message": "Processing in progress"
            }

    except Exception as e:
        logger.error(f"Failed to process motif: {e}")
        return {"error": f"Failed to process motif: {str(e)}"}


def tool_get_processing_status(args: dict) -> dict:
    """Check the status of an async processing job."""
    job_id = args.get('job_id', '').strip()

    if not job_id:
        return {"error": "job_id is required"}

    db = get_db()

    job = db.execute(
        """
        SELECT job_id, job_type, status, input_format, result_entry_ids, error_message,
               created_at, updated_at, completed_at
        FROM processing_jobs
        WHERE job_id = ?
        """,
        (job_id,)
    ).fetchone()

    if not job:
        return {"error": f"Job not found: {job_id}"}

    result = {
        "job_id": job['job_id'],
        "job_type": job['job_type'],
        "status": job['status'],
        "created_at": job['created_at'],
        "updated_at": job['updated_at']
    }

    if job['status'] == 'completed':
        result['completed_at'] = job['completed_at']
        result['result_entry_ids'] = job['result_entry_ids'].split(',') if job['result_entry_ids'] else []

    if job['status'] == 'failed':
        result['error'] = job['error_message']

    return result


def tool_check_connection(args: dict) -> dict:
    """Diagnostic tool to check MCP connection and user state."""
    from .auth import get_user_installation_token, _get_db as get_auth_db

    result = {
        "mcp_auth": {},
        "github_app": {},
        "database": {},
        "recommendations": []
    }

    # Check MCP authentication
    if hasattr(g, 'mcp_user') and g.mcp_user:
        result["mcp_auth"]["authenticated"] = True
        result["mcp_auth"]["user_id"] = g.mcp_user.get('user_id')
        result["mcp_auth"]["github_login"] = g.mcp_user.get('sub')
        result["mcp_auth"]["github_id"] = g.mcp_user.get('github_id')

        # Show if canonical user_id lookup was performed
        # Note: The middleware already resolved canonical user_id before this runs
        auth_db = get_auth_db()
        github_id = g.mcp_user.get('github_id')
        if github_id:
            canonical = auth_db.execute(
                "SELECT user_id FROM users WHERE github_id = ?", (github_id,)
            ).fetchone()
            if canonical:
                result["mcp_auth"]["canonical_user_id"] = canonical['user_id']
                if canonical['user_id'] != g.mcp_user.get('user_id'):
                    result["mcp_auth"]["user_id_corrected"] = True
    else:
        result["mcp_auth"]["authenticated"] = False
        result["recommendations"].append("MCP authentication failed - re-authenticate the MCP client")
        return result

    user_id = g.mcp_user.get('user_id')
    github_login = g.mcp_user.get('sub')

    # Check user_repos table for library configuration
    auth_db = get_auth_db()
    user_repo = auth_db.execute(
        """
        SELECT repo_full_name, installation_id, created_at
        FROM user_repos
        WHERE user_id = ? AND repo_type = 'library'
        """,
        (user_id,)
    ).fetchone()

    if user_repo:
        result["github_app"]["library_configured"] = True
        result["github_app"]["library_repo"] = user_repo['repo_full_name']
        result["github_app"]["installation_id"] = user_repo['installation_id']

        # Try to get installation token
        token = get_user_installation_token(user_id, 'library')
        if token:
            result["github_app"]["token_valid"] = True
        else:
            result["github_app"]["token_valid"] = False
            result["recommendations"].append("GitHub App token is invalid - the installation may have been removed. Re-install the GitHub App via the web interface.")
    else:
        result["github_app"]["library_configured"] = False
        result["recommendations"].append(f"No library repo configured for user {user_id}. Complete GitHub App setup via the Legato web interface.")

        # Check if there's a user record at all
        user_record = auth_db.execute(
            "SELECT github_id, github_login FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if user_record:
            result["database"]["user_exists"] = True
            result["database"]["db_github_login"] = user_record['github_login']
        else:
            result["database"]["user_exists"] = False
            result["recommendations"].append("User record not found in database - this is unexpected")

    # Check for Anthropic API key (needed for process_motif)
    from .auth import get_user_api_key
    try:
        api_key = get_user_api_key(user_id, 'anthropic')
        if api_key:
            result["database"]["anthropic_api_key_set"] = True
        else:
            result["database"]["anthropic_api_key_set"] = False
            result["recommendations"].append("Anthropic API key not configured. Add it in Legato Settings to enable process_motif.")
    except Exception as e:
        result["database"]["anthropic_api_key_set"] = False
        result["database"]["api_key_error"] = str(e)

    # Count notes in library
    try:
        user_db = get_db()  # Gets user's legato database
        note_count = user_db.execute(
            "SELECT COUNT(*) as count FROM knowledge_entries"
        ).fetchone()
        result["database"]["note_count"] = note_count['count'] if note_count else 0
    except Exception as e:
        result["database"]["note_count"] = "error"
        result["database"]["note_count_error"] = str(e)

    return result


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
