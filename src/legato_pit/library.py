"""
Library Blueprint - Knowledge Base Browser

Provides web UI for browsing and searching the knowledge base.
"""

import os
import logging
from typing import Optional

import markdown
from flask import (
    Blueprint, render_template, request, jsonify,
    current_app, g
)

from .core import login_required

logger = logging.getLogger(__name__)

library_bp = Blueprint('library', __name__, url_prefix='/library')


def get_db():
    """Get database connection."""
    if 'db_conn' not in g:
        from .rag.database import init_db
        g.db_conn = init_db()
    return g.db_conn


def render_markdown(content: str) -> str:
    """Render markdown content to HTML."""
    md = markdown.Markdown(
        extensions=[
            'tables',
            'fenced_code',
            'codehilite',
            'toc',
            'nl2br',
        ],
        extension_configs={
            'codehilite': {
                'css_class': 'highlight',
                'linenums': False,
            },
        },
    )
    return md.convert(content)


@library_bp.route('/')
@login_required
def index():
    """Library browser main page."""
    db = get_db()

    # Get categories
    categories = db.execute(
        """
        SELECT category, COUNT(*) as count
        FROM knowledge_entries
        GROUP BY category
        ORDER BY count DESC
        """
    ).fetchall()

    # Get recent entries
    recent = db.execute(
        """
        SELECT entry_id, title, category, created_at
        FROM knowledge_entries
        ORDER BY created_at DESC
        LIMIT 10
        """
    ).fetchall()

    # Get total counts
    total_knowledge = db.execute(
        "SELECT COUNT(*) FROM knowledge_entries"
    ).fetchone()[0]

    total_projects = db.execute(
        "SELECT COUNT(*) FROM project_entries"
    ).fetchone()[0]

    return render_template(
        'library.html',
        categories=[dict(c) for c in categories],
        recent=[dict(r) for r in recent],
        total_knowledge=total_knowledge,
        total_projects=total_projects,
    )


@library_bp.route('/entry/<path:entry_id>')
@login_required
def view_entry(entry_id: str):
    """View a single knowledge entry."""
    db = get_db()

    entry = db.execute(
        """
        SELECT * FROM knowledge_entries
        WHERE entry_id = ?
        """,
        (entry_id,),
    ).fetchone()

    if not entry:
        return render_template(
            'error.html',
            title="Not Found",
            message=f"Entry '{entry_id}' not found",
        ), 404

    entry_dict = dict(entry)
    entry_dict['content_html'] = render_markdown(entry_dict['content'])

    return render_template('library_entry.html', entry=entry_dict)


@library_bp.route('/category/<category>')
@login_required
def view_category(category: str):
    """View entries in a category."""
    db = get_db()

    entries = db.execute(
        """
        SELECT entry_id, title, created_at
        FROM knowledge_entries
        WHERE category = ?
        ORDER BY title
        """,
        (category,),
    ).fetchall()

    return render_template(
        'library_category.html',
        category=category,
        entries=[dict(e) for e in entries],
    )


@library_bp.route('/search')
@login_required
def search():
    """Search the library."""
    query = request.args.get('q', '')

    if not query:
        return render_template('library_search.html', query='', results=[])

    db = get_db()

    # Simple text search (SQLite FTS would be better for large datasets)
    results = db.execute(
        """
        SELECT entry_id, title, category, content,
               (CASE WHEN title LIKE ? THEN 2 ELSE 0 END +
                CASE WHEN content LIKE ? THEN 1 ELSE 0 END) as relevance
        FROM knowledge_entries
        WHERE title LIKE ? OR content LIKE ?
        ORDER BY relevance DESC, title
        LIMIT 50
        """,
        (f'%{query}%', f'%{query}%', f'%{query}%', f'%{query}%'),
    ).fetchall()

    return render_template(
        'library_search.html',
        query=query,
        results=[dict(r) for r in results],
    )


@library_bp.route('/api/entries')
@login_required
def api_list_entries():
    """API: List all entries.

    Query params:
    - category: Filter by category
    - limit: Max results (default 100)
    - offset: Pagination offset
    """
    db = get_db()

    category = request.args.get('category')
    limit = int(request.args.get('limit', 100))
    offset = int(request.args.get('offset', 0))

    if category:
        rows = db.execute(
            """
            SELECT entry_id, title, category, created_at, updated_at
            FROM knowledge_entries
            WHERE category = ?
            ORDER BY title
            LIMIT ? OFFSET ?
            """,
            (category, limit, offset),
        ).fetchall()
    else:
        rows = db.execute(
            """
            SELECT entry_id, title, category, created_at, updated_at
            FROM knowledge_entries
            ORDER BY title
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()

    return jsonify({
        'entries': [dict(r) for r in rows],
        'limit': limit,
        'offset': offset,
    })


@library_bp.route('/api/entry/<entry_id>')
@login_required
def api_get_entry(entry_id: str):
    """API: Get a single entry."""
    db = get_db()

    entry = db.execute(
        "SELECT * FROM knowledge_entries WHERE entry_id = ?",
        (entry_id,),
    ).fetchone()

    if not entry:
        return jsonify({'error': 'Not found'}), 404

    return jsonify(dict(entry))


@library_bp.route('/api/sync', methods=['POST'])
@login_required
def api_sync():
    """Trigger sync from Library repository.

    Request body (optional):
    {
        "source": "github",  # or "filesystem"
        "repo": "bobbyhiddn/Legato.Library",
        "path": "/path/to/library"  # for filesystem
    }

    Response:
    {
        "status": "success",
        "stats": {
            "files_found": 21,
            "entries_created": 15,
            "entries_updated": 6,
            "embeddings_generated": 21
        }
    }
    """
    from .rag.library_sync import LibrarySync
    from .rag.embedding_service import EmbeddingService
    from .rag.openai_provider import OpenAIEmbeddingProvider

    data = request.get_json() or {}
    source = data.get('source', 'github')

    try:
        db = get_db()

        # Create embedding service for generating embeddings
        try:
            provider = OpenAIEmbeddingProvider()
            embedding_service = EmbeddingService(provider, db)
        except ValueError:
            embedding_service = None  # Will skip embedding generation

        sync = LibrarySync(db, embedding_service)

        if source == 'filesystem':
            path = data.get('path', '/mnt/d/Code/Legato/Legato.Library')
            stats = sync.sync_from_filesystem(path)
        else:
            repo = data.get('repo', 'bobbyhiddn/Legato.Library')
            stats = sync.sync_from_github(repo)

        return jsonify({
            'status': 'success',
            'stats': stats,
        })

    except Exception as e:
        logger.error(f"Sync failed: {e}")
        return jsonify({
            'status': 'error',
            'error': str(e),
        }), 500


@library_bp.route('/api/sync/status', methods=['GET'])
@login_required
def api_sync_status():
    """Get the latest sync status."""
    from .rag.library_sync import LibrarySync

    try:
        db = get_db()
        sync = LibrarySync(db)
        status = sync.get_sync_status()
        return jsonify(status)
    except Exception as e:
        logger.error(f"Get sync status failed: {e}")
        return jsonify({'error': str(e)}), 500


@library_bp.route('/projects')
@login_required
def list_projects():
    """List all projects."""
    db = get_db()

    projects = db.execute(
        """
        SELECT project_id, repo_name, title, description, status, created_at
        FROM project_entries
        ORDER BY created_at DESC
        """
    ).fetchall()

    return render_template(
        'library_projects.html',
        projects=[dict(p) for p in projects],
    )
