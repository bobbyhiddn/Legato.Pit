"""
Library Blueprint - Knowledge Base Browser

Provides web UI for browsing and searching the knowledge base.
"""

import os
import logging
import threading
from datetime import datetime
from typing import Optional

import markdown
from flask import (
    Blueprint, render_template, request, jsonify,
    current_app, g
)

from .core import login_required

logger = logging.getLogger(__name__)

library_bp = Blueprint('library', __name__, url_prefix='/library')

# Track if a background sync is already running
_sync_lock = threading.Lock()
_sync_in_progress = False


def get_db():
    """Get legato database connection (knowledge entries)."""
    if 'legato_db_conn' not in g:
        from .rag.database import init_db
        g.legato_db_conn = init_db()
        # Force WAL checkpoint to ensure we see latest writes
        g.legato_db_conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    return g.legato_db_conn


def get_agents_db():
    """Get agents database connection (agent queue)."""
    if 'agents_db_conn' not in g:
        from .rag.database import init_agents_db
        g.agents_db_conn = init_agents_db()
    return g.agents_db_conn


def trigger_background_sync():
    """Trigger a background library sync if not already running.

    This is called when the library page is loaded to ensure fresh data.
    """
    global _sync_in_progress

    with _sync_lock:
        if _sync_in_progress:
            return  # Sync already running
        _sync_in_progress = True

    def do_sync():
        global _sync_in_progress
        try:
            from flask import current_app
            from .rag.database import init_db
            from .rag.library_sync import LibrarySync
            from .rag.embedding_service import EmbeddingService
            from .rag.openai_provider import OpenAIEmbeddingProvider

            token = os.environ.get('SYSTEM_PAT')
            if not token:
                logger.warning("SYSTEM_PAT not set, skipping on-load sync")
                return

            db = init_db()

            embedding_service = None
            if os.environ.get('OPENAI_API_KEY'):
                try:
                    provider = OpenAIEmbeddingProvider()
                    embedding_service = EmbeddingService(provider, db)
                except Exception as e:
                    logger.warning(f"Could not create embedding service: {e}")

            sync = LibrarySync(db, embedding_service)
            stats = sync.sync_from_github('bobbyhiddn/Legato.Library', token=token)

            if stats.get('entries_created', 0) > 0 or stats.get('entries_updated', 0) > 0:
                logger.info(f"On-load library sync: {stats}")

        except Exception as e:
            logger.error(f"On-load library sync failed: {e}")
        finally:
            with _sync_lock:
                _sync_in_progress = False

    # Run sync in background thread
    thread = threading.Thread(target=do_sync, daemon=True)
    thread.start()


def should_sync() -> bool:
    """Check if library sync is needed based on last sync time.

    Returns True if last sync was more than 60 seconds ago or never happened.
    """
    try:
        db = get_db()
        row = db.execute(
            """
            SELECT synced_at FROM sync_log
            ORDER BY synced_at DESC LIMIT 1
            """
        ).fetchone()

        if not row:
            return True  # Never synced

        # Parse the timestamp
        last_sync = datetime.fromisoformat(row['synced_at'].replace('Z', '+00:00'))
        now = datetime.now(last_sync.tzinfo) if last_sync.tzinfo else datetime.now()

        # Sync if more than 60 seconds have passed
        return (now - last_sync).total_seconds() > 60

    except Exception as e:
        logger.warning(f"Error checking sync status: {e}")
        return True  # Sync on error


def strip_frontmatter(content: str) -> str:
    """Remove YAML frontmatter from markdown content."""
    if content.startswith('---'):
        parts = content.split('---', 2)
        if len(parts) >= 3:
            return parts[2].strip()
    return content


def render_markdown(content: str) -> str:
    """Render markdown content to HTML, stripping frontmatter."""
    # Remove frontmatter since it's shown in collapsible metadata
    content = strip_frontmatter(content)

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
    # Trigger background sync if needed (doesn't block page load)
    if should_sync():
        trigger_background_sync()

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

    # Check if a Chord was spawned from this Note (agent_queue is in agents.db)
    agents_db = get_agents_db()
    chord_info = agents_db.execute(
        """
        SELECT queue_id, project_name, status, approved_at
        FROM agent_queue
        WHERE source_transcript = ?
        AND status = 'approved'
        ORDER BY approved_at DESC
        LIMIT 1
        """,
        (f"library:{entry_id}",),
    ).fetchone()

    if chord_info:
        org = current_app.config.get('LEGATO_ORG', 'bobbyhiddn')
        entry_dict['chord'] = {
            'name': chord_info['project_name'],
            'repo': f"Lab.{chord_info['project_name']}.Chord",
            'url': f"https://github.com/{org}/Lab.{chord_info['project_name']}.Chord",
            'approved_at': chord_info['approved_at'],
        }

    return render_template('library_entry.html', entry=entry_dict)


@library_bp.route('/category/<category>')
@login_required
def view_category(category: str):
    """View entries in a category."""
    db = get_db()

    entries = db.execute(
        """
        SELECT entry_id, title, created_at, needs_chord, chord_status
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


@library_bp.route('/api/dedupe', methods=['POST'])
@login_required
def api_dedupe():
    """Remove duplicate and invalid entries.

    Removes:
    1. Duplicates by file_path (keeps most recent)
    2. Entries with non-standard entry_ids (not kb-xxxxxxxx format)

    Response:
    {
        "status": "success",
        "duplicates_removed": 5,
        "invalid_removed": 3
    }
    """
    import re

    try:
        db = get_db()

        # 1. Remove entries with invalid entry_ids (not kb-xxxxxxxx format)
        invalid = db.execute(
            """
            SELECT id, entry_id, file_path
            FROM knowledge_entries
            WHERE entry_id NOT LIKE 'kb-%'
            OR LENGTH(entry_id) != 11
            """
        ).fetchall()

        invalid_count = 0
        for inv in invalid:
            db.execute("DELETE FROM embeddings WHERE entry_id = ? AND entry_type = 'knowledge'", (inv['id'],))
            db.execute("DELETE FROM knowledge_entries WHERE id = ?", (inv['id'],))
            invalid_count += 1
            logger.info(f"Removed invalid entry: {inv['entry_id']}")

        # 2. Find duplicates by file_path (keep the one with highest id = most recent)
        duplicates = db.execute(
            """
            SELECT id, file_path, entry_id
            FROM knowledge_entries
            WHERE file_path IS NOT NULL
            AND id NOT IN (
                SELECT MAX(id)
                FROM knowledge_entries
                WHERE file_path IS NOT NULL
                GROUP BY file_path
            )
            """
        ).fetchall()

        dup_count = 0
        for dup in duplicates:
            db.execute("DELETE FROM embeddings WHERE entry_id = ? AND entry_type = 'knowledge'", (dup['id'],))
            db.execute("DELETE FROM knowledge_entries WHERE id = ?", (dup['id'],))
            dup_count += 1
            logger.info(f"Removed duplicate: {dup['entry_id']} ({dup['file_path']})")

        db.commit()

        return jsonify({
            'status': 'success',
            'duplicates_removed': dup_count,
            'invalid_removed': invalid_count,
            'total_removed': dup_count + invalid_count,
        })

    except Exception as e:
        logger.error(f"Dedupe failed: {e}")
        return jsonify({
            'status': 'error',
            'error': str(e),
        }), 500


@library_bp.route('/api/generate-embeddings', methods=['POST'])
@login_required
def api_generate_embeddings():
    """Generate embeddings for entries that don't have them.

    This is useful if sync completed but embeddings weren't generated
    (e.g., missing API key at sync time).

    Response:
    {
        "status": "success",
        "embeddings_generated": 42
    }
    """
    from .rag.embedding_service import EmbeddingService
    from .rag.openai_provider import OpenAIEmbeddingProvider

    try:
        db = get_db()

        # Create embedding service
        try:
            provider = OpenAIEmbeddingProvider()
            embedding_service = EmbeddingService(provider, db)
        except ValueError as e:
            return jsonify({
                'status': 'error',
                'error': f'OpenAI API key not configured: {e}',
            }), 400

        # Generate missing embeddings
        count = embedding_service.generate_missing_embeddings('knowledge', delay=0.1)

        return jsonify({
            'status': 'success',
            'embeddings_generated': count,
        })

    except Exception as e:
        logger.error(f"Generate embeddings failed: {e}")
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


@library_bp.route('/api/create-chord', methods=['POST'])
@login_required
def api_create_chord():
    """Create a Chord from one or more Notes.

    Request body:
    {
        "entry_ids": ["kb-abc123", "kb-def456"],
        "project_name": "my-project"
    }

    Response:
    {
        "status": "queued",
        "queue_id": "aq-xyz789",
        "project_name": "my-project",
        "source_count": 2
    }
    """
    import json
    import re
    import secrets

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    entry_ids = data.get('entry_ids', [])
    project_name = data.get('project_name', '')

    if not entry_ids:
        return jsonify({'error': 'entry_ids is required'}), 400

    if not project_name:
        return jsonify({'error': 'project_name is required'}), 400

    # Validate project name
    project_name = re.sub(r'[^a-z0-9-]', '', project_name.lower())[:30]
    if len(project_name) < 2:
        return jsonify({'error': 'Project name must be at least 2 characters'}), 400

    try:
        legato_db = get_db()
        agents_db = get_agents_db()

        # Fetch all entries
        placeholders = ','.join('?' * len(entry_ids))
        entries = legato_db.execute(
            f"""
            SELECT entry_id, title, category, content
            FROM knowledge_entries
            WHERE entry_id IN ({placeholders})
            """,
            entry_ids
        ).fetchall()

        if not entries:
            return jsonify({'error': 'No entries found'}), 404

        entries = [dict(e) for e in entries]

        # Build combined tasker body
        combined_title = f"Multi-note Chord: {project_name}"
        if len(entries) == 1:
            combined_title = entries[0]['title']

        context_sections = []
        for entry in entries:
            content_preview = entry['content'][:400] if entry['content'] else ''
            context_sections.append(
                f"### From: {entry['title']} ({entry['entry_id']})\n"
                f'"{content_preview}..."'
            )

        tasker_body = f"""## Tasker: {combined_title}

### Context
This chord combines {len(entries)} note(s) from the Library:

{chr(10).join(context_sections)}

### Objective
Implement the project incorporating ideas from all source notes.

### Acceptance Criteria
- [ ] Core functionality implemented
- [ ] All source note concepts addressed
- [ ] Documentation updated
- [ ] Tests written

### Constraints
- Follow patterns in `copilot-instructions.md`
- Reference `SIGNAL.md` for project intent
- Keep PRs focused and reviewable

### References
{chr(10).join(f'- Source: `{e["entry_id"]}`' for e in entries)}

---
*Generated from {len(entries)} Library note(s) | Combined Chord*
"""

        # Build signal JSON
        signal_json = {
            "id": f"lab.chord.{project_name}",
            "type": "project",
            "source": "pit-library-multi",
            "category": "chord",
            "title": combined_title,
            "domain_tags": [],
            "intent": f"Combined from {len(entries)} notes",
            "key_phrases": [],
            "source_entries": [e['entry_id'] for e in entries],
        }

        # Generate queue ID
        queue_id = f"aq-{secrets.token_hex(6)}"

        # Store all entry IDs as comma-separated in related_entry_id
        related_ids = ','.join(e['entry_id'] for e in entries)

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
                'chord',
                combined_title,
                f"Combined from {len(entries)} notes",
                json.dumps(signal_json),
                tasker_body,
                f"library:multi:{len(entries)}",
                related_ids,
            )
        )
        agents_db.commit()

        logger.info(f"Queued multi-note chord: {queue_id} - {project_name} from {len(entries)} notes")

        return jsonify({
            'status': 'queued',
            'queue_id': queue_id,
            'project_name': project_name,
            'source_count': len(entries),
        })

    except Exception as e:
        logger.error(f"Failed to create multi-note chord: {e}")
        return jsonify({'error': str(e)}), 500


@library_bp.route('/api/entry/<path:entry_id>/save', methods=['POST'])
@login_required
def api_save_entry(entry_id: str):
    """Save entry to GitHub and update local DB.

    Request body:
    {
        "content": "markdown content",
        "message": "commit message"
    }

    Response:
    {
        "status": "success",
        "commit_sha": "abc123..."
    }
    """
    from .rag.github_service import commit_file

    data = request.get_json()
    if not data:
        return jsonify({'status': 'error', 'error': 'No data provided'}), 400

    content = data.get('content')
    message = data.get('message', 'Update knowledge entry')

    if not content:
        return jsonify({'status': 'error', 'error': 'Content is required'}), 400

    try:
        db = get_db()

        # Get entry from DB
        entry = db.execute(
            "SELECT * FROM knowledge_entries WHERE entry_id = ?",
            (entry_id,)
        ).fetchone()

        if not entry:
            return jsonify({'status': 'error', 'error': 'Entry not found'}), 404

        entry_dict = dict(entry)
        file_path = entry_dict.get('file_path')

        if not file_path:
            return jsonify({
                'status': 'error',
                'error': 'Entry has no file_path - cannot commit to GitHub'
            }), 400

        # Get token and repo config
        token = current_app.config.get('SYSTEM_PAT')
        if not token:
            return jsonify({
                'status': 'error',
                'error': 'SYSTEM_PAT not configured'
            }), 500

        repo = 'bobbyhiddn/Legato.Library'

        # Commit to GitHub
        result = commit_file(
            repo=repo,
            path=file_path,
            content=content,
            message=message,
            token=token,
        )

        commit_sha = result['commit']['sha']

        # Update local DB
        db.execute(
            """
            UPDATE knowledge_entries
            SET content = ?, updated_at = CURRENT_TIMESTAMP
            WHERE entry_id = ?
            """,
            (content, entry_id)
        )
        db.commit()

        logger.info(f"Saved entry {entry_id} to GitHub: {commit_sha[:7]}")

        return jsonify({
            'status': 'success',
            'commit_sha': commit_sha,
        })

    except Exception as e:
        logger.error(f"Save entry failed: {e}")
        return jsonify({
            'status': 'error',
            'error': str(e),
        }), 500
