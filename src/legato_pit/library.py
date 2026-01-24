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
    current_app, g, session
)

from .core import login_required, library_required

logger = logging.getLogger(__name__)

library_bp = Blueprint('library', __name__, url_prefix='/library')

# Track if a background sync is already running
_sync_lock = threading.Lock()
_sync_in_progress = False


def get_db():
    """Get legato database connection for current user."""
    from .rag.database import get_user_legato_db
    return get_user_legato_db()


def get_agents_db():
    """Get agents database connection (agent queue)."""
    if 'agents_db_conn' not in g:
        from .rag.database import init_agents_db
        g.agents_db_conn = init_agents_db()
    return g.agents_db_conn


def get_categories_with_counts():
    """Get all user categories with entry counts, including orphaned/inactive categories.

    Returns categories from user_categories table (not just categories with entries),
    with a count of how many entries exist in each category.

    Also detects:
    - "inactive" categories - exist in user_categories but is_active=0
    - "orphaned" categories - used in knowledge_entries but not in user_categories at all

    Returns:
        List of dicts with: category (name), display_name, count, folder_name, color, orphaned, inactive
    """
    from flask import session
    from .rag.database import get_user_categories

    db = get_db()
    user_id = session.get('user', {}).get('user_id') or 'default'
    user_categories = get_user_categories(db, user_id)  # Only active categories

    # Also get inactive categories
    inactive_categories = db.execute("""
        SELECT id, name, display_name, folder_name, color
        FROM user_categories
        WHERE user_id = ? AND is_active = 0
    """, (user_id,)).fetchall()

    # Get entry counts per category
    counts = db.execute(
        """
        SELECT category, COUNT(*) as count
        FROM knowledge_entries
        WHERE category IS NOT NULL AND category != ''
        GROUP BY category
        """
    ).fetchall()
    count_map = {row['category']: row['count'] for row in counts}

    # Track all known categories (active + inactive)
    defined_categories = {cat['name'] for cat in user_categories}
    defined_categories.update(cat['name'] for cat in inactive_categories)

    # Merge active categories with counts
    result = []
    for cat in user_categories:
        result.append({
            'category': cat['name'],
            'display_name': cat['display_name'],
            'count': count_map.get(cat['name'], 0),
            'folder_name': cat['folder_name'],
            'color': cat.get('color', '#6366f1'),
            'orphaned': False,
            'inactive': False,
        })

    # Auto-reactivate inactive categories that have entries
    for cat in inactive_categories:
        count = count_map.get(cat['name'], 0)
        if count > 0:  # Has entries - auto-reactivate
            try:
                db.execute("""
                    UPDATE user_categories
                    SET is_active = 1, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (cat['id'],))
                db.commit()

                logger.info(f"Auto-reactivated category: '{cat['name']}' ({count} entries)")

                # Add as regular category
                result.append({
                    'category': cat['name'],
                    'display_name': cat['display_name'],
                    'count': count,
                    'folder_name': cat['folder_name'],
                    'color': cat['color'] or '#9ca3af',
                    'orphaned': False,
                    'inactive': False,
                })
                defined_categories.add(cat['name'])

            except Exception as e:
                logger.error(f"Failed to auto-reactivate category '{cat['name']}': {e}")
                # Fall back to showing as inactive
                result.append({
                    'category': cat['name'],
                    'display_name': cat['display_name'],
                    'count': count,
                    'folder_name': cat['folder_name'],
                    'color': cat['color'] or '#9ca3af',
                    'orphaned': True,
                    'inactive': True,
                })

    # Find truly orphaned categories (in entries but not in user_categories at all)
    # Auto-adopt them to ensure permanence
    for category_name, count in count_map.items():
        if category_name and category_name not in defined_categories:
            # Auto-create the category record
            display_name = category_name.replace('-', ' ').title()
            folder_name = f"{category_name}s"

            try:
                max_order = db.execute(
                    "SELECT MAX(sort_order) FROM user_categories WHERE user_id = ?",
                    (user_id,)
                ).fetchone()[0] or 0

                db.execute("""
                    INSERT INTO user_categories (user_id, name, display_name, description, folder_name, sort_order, color)
                    VALUES (?, ?, ?, 'Auto-created from existing entries', ?, ?, '#9ca3af')
                """, (user_id, category_name, display_name, folder_name, max_order + 1))
                db.commit()

                logger.info(f"Auto-adopted orphaned category: '{category_name}' ({count} entries)")

                # Add as regular category now
                result.append({
                    'category': category_name,
                    'display_name': display_name,
                    'count': count,
                    'folder_name': folder_name,
                    'color': '#9ca3af',
                    'orphaned': False,
                    'inactive': False,
                })
                defined_categories.add(category_name)

            except Exception as e:
                logger.error(f"Failed to auto-adopt category '{category_name}': {e}")
                # Fall back to showing as orphaned
                result.append({
                    'category': category_name,
                    'display_name': display_name,
                    'count': count,
                    'folder_name': folder_name,
                    'color': '#9ca3af',
                    'orphaned': True,
                    'inactive': False,
                })

    return result


def trigger_background_sync():
    """Trigger a background library sync if not already running.

    This is called when the library page is loaded to ensure fresh data.
    Also cleans up orphaned chord references.
    """
    global _sync_in_progress

    with _sync_lock:
        if _sync_in_progress:
            return  # Sync already running
        _sync_in_progress = True

    def do_sync():
        global _sync_in_progress
        try:
            from flask import current_app, session
            from .rag.database import init_db
            from .rag.library_sync import LibrarySync
            from .rag.embedding_service import EmbeddingService
            from .rag.openai_provider import OpenAIEmbeddingProvider
            from .chords import fetch_chord_repos

            # Get user-specific database in multi-tenant mode
            mode = current_app.config.get('LEGATO_MODE', 'single-tenant')
            user = session.get('user', {})
            user_id = user.get('user_id')

            if mode == 'multi-tenant':
                db = init_db(user_id=user_id) if user_id else init_db()
            else:
                db = init_db()

            # Get user installation token - required in multi-tenant mode
            token = None
            if user_id:
                from .auth import get_user_installation_token
                token = get_user_installation_token(user_id, 'library')

            # In multi-tenant mode, require user token - don't fall back to SYSTEM_PAT
            if not token:
                logger.warning("No user token available, skipping on-load sync")
                return

            embedding_service = None
            if os.environ.get('OPENAI_API_KEY'):
                try:
                    provider = OpenAIEmbeddingProvider()
                    embedding_service = EmbeddingService(provider, db)
                except Exception as e:
                    logger.warning(f"Could not create embedding service: {e}")

            from .core import get_user_library_repo
            library_repo = get_user_library_repo()
            sync = LibrarySync(db, embedding_service)
            stats = sync.sync_from_github(library_repo, token=token)

            if stats.get('entries_created', 0) > 0 or stats.get('entries_updated', 0) > 0:
                logger.info(f"On-load library sync: {stats}")

            # Cleanup orphaned chord references - use user's org
            org = user.get('username') or os.environ.get('LEGATO_ORG')
            if not org:
                logger.debug("No org available for chord cleanup")
                return
            try:
                notes_with_chords = db.execute(
                    """
                    SELECT entry_id, chord_repo FROM knowledge_entries
                    WHERE chord_status = 'active' AND chord_repo IS NOT NULL
                    """
                ).fetchall()

                if notes_with_chords:
                    valid_repos = fetch_chord_repos(token, org)
                    valid_repo_names = {r['name'] for r in valid_repos}

                    orphan_count = 0
                    for note in notes_with_chords:
                        chord_repo = note['chord_repo']
                        repo_name = chord_repo.split('/')[-1] if '/' in chord_repo else chord_repo
                        if repo_name not in valid_repo_names:
                            db.execute(
                                """
                                UPDATE knowledge_entries
                                SET chord_status = NULL, chord_repo = NULL, chord_id = NULL
                                WHERE entry_id = ?
                                """,
                                (note['entry_id'],)
                            )
                            orphan_count += 1

                    if orphan_count > 0:
                        db.commit()
                        logger.info(f"Cleaned up {orphan_count} orphaned chord references")

            except Exception as e:
                logger.warning(f"Orphan cleanup failed: {e}")

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
@library_required
def index():
    """Library browser main page."""
    # Trigger background sync if needed (doesn't block page load)
    if should_sync():
        trigger_background_sync()

    db = get_db()

    # Get categories from user_categories with entry counts
    categories = get_categories_with_counts()

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
        categories=categories,
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

    import json
    entry_dict = dict(entry)
    entry_dict['content_html'] = render_markdown(entry_dict['content'])

    # Parse JSON tags if present
    if entry_dict.get('domain_tags'):
        try:
            entry_dict['domain_tags'] = json.loads(entry_dict['domain_tags'])
        except (json.JSONDecodeError, TypeError):
            entry_dict['domain_tags'] = []
    else:
        entry_dict['domain_tags'] = []

    if entry_dict.get('key_phrases'):
        try:
            entry_dict['key_phrases'] = json.loads(entry_dict['key_phrases'])
        except (json.JSONDecodeError, TypeError):
            entry_dict['key_phrases'] = []
    else:
        entry_dict['key_phrases'] = []

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
        # Use user's org for chord URL
        user = session.get('user', {})
        org = user.get('username') or current_app.config.get('LEGATO_ORG')
        entry_dict['chord'] = {
            'name': chord_info['project_name'],
            'repo': f"{chord_info['project_name']}.Chord",
            'url': f"https://github.com/{org}/{chord_info['project_name']}.Chord" if org else None,
            'approved_at': chord_info['approved_at'],
        }

    # Get categories for sidebar (from user_categories with counts)
    categories = get_categories_with_counts()

    return render_template(
        'library_entry.html',
        entry=entry_dict,
        categories=categories,
    )


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

    # Get categories for sidebar (from user_categories with counts)
    categories = get_categories_with_counts()

    return render_template(
        'library_category.html',
        category=category,
        entries=[dict(e) for e in entries],
        categories=categories,
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


@library_bp.route('/daily')
@login_required
def daily_index():
    """Show daily view with date picker and recent dates."""
    db = get_db()

    # Get dates with note counts
    dates = db.execute(
        """
        SELECT DATE(created_at) as date, COUNT(*) as count
        FROM knowledge_entries
        GROUP BY DATE(created_at)
        ORDER BY date DESC
        LIMIT 30
        """
    ).fetchall()

    # Get categories for sidebar (from user_categories with counts)
    categories = get_categories_with_counts()

    return render_template(
        'library_daily.html',
        dates=[dict(d) for d in dates],
        categories=categories,
    )


@library_bp.route('/daily/<date>')
@login_required
def daily_view(date: str):
    """View notes from a specific date."""
    db = get_db()

    # Validate date format
    try:
        datetime.strptime(date, '%Y-%m-%d')
    except ValueError:
        return "Invalid date format. Use YYYY-MM-DD", 400

    # Get entries for this date
    entries = db.execute(
        """
        SELECT entry_id, title, category, created_at, chord_status
        FROM knowledge_entries
        WHERE DATE(created_at) = ?
        ORDER BY created_at DESC
        """,
        (date,)
    ).fetchall()

    # Get categories for sidebar (from user_categories with counts)
    categories = get_categories_with_counts()

    # Get adjacent dates for navigation
    prev_date = db.execute(
        """
        SELECT DATE(created_at) as date FROM knowledge_entries
        WHERE DATE(created_at) < ?
        ORDER BY date DESC LIMIT 1
        """,
        (date,)
    ).fetchone()

    next_date = db.execute(
        """
        SELECT DATE(created_at) as date FROM knowledge_entries
        WHERE DATE(created_at) > ?
        ORDER BY date ASC LIMIT 1
        """,
        (date,)
    ).fetchone()

    return render_template(
        'library_daily_view.html',
        date=date,
        entries=[dict(e) for e in entries],
        categories=categories,
        prev_date=prev_date['date'] if prev_date else None,
        next_date=next_date['date'] if next_date else None,
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
            from .core import get_user_library_repo
            from .auth import get_user_installation_token
            from flask import session

            default_repo = get_user_library_repo()
            repo = data.get('repo', default_repo)

            # Get user's installation token for multi-tenant mode
            user_id = session.get('user', {}).get('user_id')
            token = None
            if user_id:
                token = get_user_installation_token(user_id, 'library')
                if not token:
                    logger.warning(f"No installation token for user {user_id}")
                    return jsonify({'error': 'GitHub authorization required'}), 401

            stats = sync.sync_from_github(repo, token=token)

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


@library_bp.route('/api/reset', methods=['POST'])
@login_required
def api_reset():
    """Clear all entries and re-sync from configured Library.

    Use this when you've connected a new Library repo and need to
    clear old data.

    Response:
    {
        "status": "success",
        "entries_deleted": 110,
        "sync_stats": {...}
    }
    """
    from flask import session
    from .rag.database import get_user_legato_db
    from .rag.library_sync import LibrarySync
    from .core import get_user_library_repo

    try:
        db = get_user_legato_db()

        # Count and delete all entries
        count = db.execute("SELECT COUNT(*) as cnt FROM knowledge_entries").fetchone()
        entries_deleted = count['cnt'] if count else 0

        db.execute("DELETE FROM knowledge_entries")
        db.execute("DELETE FROM embeddings")
        db.execute("DELETE FROM sync_log")
        db.commit()

        logger.info(f"Cleared {entries_deleted} entries for reset")

        # Re-sync from configured Library
        library_repo = get_user_library_repo()

        # Get user's installation token - required in multi-tenant mode
        from .auth import get_user_installation_token
        user_id = session.get('user', {}).get('user_id')
        token = None
        if user_id:
            token = get_user_installation_token(user_id, 'library')

        # In multi-tenant mode, require user token - don't fall back to SYSTEM_PAT
        if not token:
            logger.warning(f"No installation token for user {user_id}")
            return jsonify({'error': 'GitHub authorization required'}), 401

        embedding_service = None
        if os.environ.get('OPENAI_API_KEY'):
            try:
                from .rag.openai_provider import OpenAIEmbeddingProvider
                from .rag.embedding_service import EmbeddingService
                provider = OpenAIEmbeddingProvider()
                embedding_service = EmbeddingService(provider, db)
            except Exception as e:
                logger.warning(f"Could not create embedding service: {e}")

        sync = LibrarySync(db, embedding_service)
        stats = sync.sync_from_github(library_repo, token=token)

        return jsonify({
            'status': 'success',
            'entries_deleted': entries_deleted,
            'library_repo': library_repo,
            'sync_stats': stats,
        })

    except Exception as e:
        logger.error(f"Reset failed: {e}")
        return jsonify({
            'status': 'error',
            'error': str(e),
        }), 500


@library_bp.route('/api/dedupe', methods=['POST'])
@login_required
def api_dedupe():
    """Remove duplicate entries by file_path.

    Removes duplicates by file_path (keeps most recent).

    Response:
    {
        "status": "success",
        "duplicates_removed": 5
    }
    """
    try:
        db = get_db()

        # Find duplicates by file_path (keep the one with highest id = most recent)
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


@library_bp.route('/tasks')
@login_required
def tasks_view():
    """View notes marked as tasks in a Kanban board layout."""
    db = get_db()

    # Fetch all tasks ordered by due_date and updated_at
    sql = """
        SELECT entry_id, title, category, task_status, due_date, created_at, updated_at
        FROM knowledge_entries
        WHERE task_status IS NOT NULL
        ORDER BY due_date ASC NULLS LAST, updated_at DESC
    """
    tasks = db.execute(sql).fetchall()

    # Group tasks by status for Kanban columns
    tasks_by_status = {
        'blocked': [],
        'in_progress': [],
        'pending': [],
        'done': []
    }
    for task in tasks:
        task_dict = dict(task)
        status = task_dict.get('task_status', 'pending')
        if status in tasks_by_status:
            tasks_by_status[status].append(task_dict)

    # Get counts by status
    status_counts = {status: len(tasks) for status, tasks in tasks_by_status.items()}
    total = sum(status_counts.values())

    # Get categories for sidebar
    categories = get_categories_with_counts()

    return render_template(
        'library_tasks.html',
        tasks_by_status=tasks_by_status,
        status_counts=status_counts,
        total_tasks=total,
        categories=categories,
    )


@library_bp.route('/api/task/<path:entry_id>/status', methods=['POST'])
@login_required
def api_update_task_status(entry_id: str):
    """Update task status for a note.

    Updates both the local database AND syncs to GitHub by updating frontmatter.

    Request body:
    {
        "status": "pending" | "in_progress" | "done" | "blocked" | null,
        "due_date": "2024-01-15"  // optional
    }

    Set status to null to remove task_status from the note.
    """
    import re
    from .rag.github_service import get_file_content, commit_file

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    status = data.get('status')
    if status is not None:
        status = str(status).strip() if status else None

    due_date = data.get('due_date', '').strip() if data.get('due_date') else None

    valid_statuses = {'pending', 'in_progress', 'done', 'blocked'}
    if status is not None and status not in valid_statuses:
        return jsonify({'error': f'Invalid status. Must be one of: {", ".join(sorted(valid_statuses))}'}), 400

    try:
        db = get_db()

        # Get full entry info including file_path
        entry = db.execute(
            "SELECT entry_id, title, task_status, file_path, content FROM knowledge_entries WHERE entry_id = ?",
            (entry_id,)
        ).fetchone()

        if not entry:
            return jsonify({'error': 'Entry not found'}), 404

        old_status = entry['task_status']
        file_path = entry['file_path']

        github_updated = False

        # Sync to GitHub if file_path exists
        if file_path:
            try:
                from .core import get_user_library_repo
                from .auth import get_user_installation_token
                from flask import session

                user_id = session.get('user', {}).get('user_id')
                token = get_user_installation_token(user_id, 'library') if user_id else None
                repo = get_user_library_repo()

                if token:
                    # Get current content from GitHub
                    content = get_file_content(repo, file_path, token)
                    if content:
                        # Update frontmatter with new task_status
                        if content.startswith('---'):
                            parts = content.split('---', 2)
                            if len(parts) >= 3:
                                frontmatter = parts[1]
                                body = parts[2]

                                # Check if task_status already exists in frontmatter
                                has_task_status = re.search(r'^task_status:\s*.*$', frontmatter, re.MULTILINE)

                                if status is None:
                                    # Remove task_status from frontmatter
                                    new_frontmatter = re.sub(
                                        r'^task_status:\s*.*\n?',
                                        '',
                                        frontmatter,
                                        flags=re.MULTILINE
                                    )
                                    commit_message = f'Remove task status from {entry["title"]}'
                                elif has_task_status:
                                    # Update existing task_status
                                    new_frontmatter = re.sub(
                                        r'^task_status:\s*.*$',
                                        f'task_status: {status}',
                                        frontmatter,
                                        flags=re.MULTILINE
                                    )
                                    commit_message = f'Update task status: {old_status or "none"} -> {status}'
                                else:
                                    # Add task_status to frontmatter (before closing ---)
                                    new_frontmatter = frontmatter.rstrip() + f'\ntask_status: {status}\n'
                                    commit_message = f'Add task status: {status}'

                                new_content = f'---{new_frontmatter}---{body}'

                                # Commit to GitHub
                                commit_file(
                                    repo=repo,
                                    path=file_path,
                                    content=new_content,
                                    message=commit_message,
                                    token=token
                                )
                                github_updated = True
                                logger.info(f"Synced task status to GitHub: {file_path}")
                        else:
                            # No frontmatter exists, add it
                            if status is not None:
                                new_content = f'---\ntask_status: {status}\n---\n\n{content}'
                                commit_file(
                                    repo=repo,
                                    path=file_path,
                                    content=new_content,
                                    message=f'Add task status: {status}',
                                    token=token
                                )
                                github_updated = True
                                logger.info(f"Added frontmatter with task status to: {file_path}")

            except Exception as e:
                logger.warning(f"Could not sync task status to GitHub: {e}")
                # Continue anyway - DB will be updated

        # Update local database
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

        logger.info(f"Updated task status: {entry_id} {old_status} -> {status}")

        return jsonify({
            'status': 'success',
            'entry_id': entry_id,
            'old_status': old_status,
            'new_status': status,
            'due_date': due_date,
            'github_updated': github_updated
        })

    except Exception as e:
        logger.error(f"Update task status failed: {e}")
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


def generate_chord_summary(entries: list[dict]) -> dict:
    """Use Claude to generate a title and summary for a chord.

    Args:
        entries: List of note dicts with 'title', 'content', 'category'

    Returns:
        {
            "project_name": "slug-style-name",
            "title": "Human Readable Title",
            "summary": "What the agent should accomplish...",
            "intent": "Brief intent statement"
        }
    """
    import os
    import anthropic

    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        # Fallback if no API key
        if len(entries) == 1:
            title = entries[0]['title']
            slug = title.lower().replace(' ', '_')[:30]
            return {
                "project_name": slug,
                "title": title,
                "summary": f"Implement the concept described in: {title}",
                "intent": entries[0].get('category', 'project')
            }
        return {
            "project_name": f"chord-{len(entries)}-notes",
            "title": f"Combined Chord ({len(entries)} notes)",
            "summary": "Implement the combined concepts from the source notes.",
            "intent": "multi-note project"
        }

    # Build context for Claude
    notes_text = ""
    for i, entry in enumerate(entries, 1):
        content = entry.get('content', '')[:2000]  # Limit per note
        notes_text += f"""
### Note {i}: {entry['title']}
Category: {entry.get('category', 'uncategorized')}

{content}

---
"""

    prompt = f"""Analyze these knowledge notes and generate a project summary for a coding agent.

{notes_text}

Based on these notes, provide:
1. A short project slug (lowercase, hyphens, max 30 chars) - this will be the GitHub repo/project name
2. A human-readable title (max 60 chars)
3. A clear summary (2-4 sentences) describing what the coding agent should implement. Be specific about the expected output.
4. A brief intent phrase (3-5 words)

Respond in this exact JSON format:
{{
    "project_name": "example-project-slug",
    "title": "Example Project Title",
    "summary": "The agent should implement X by doing Y. The expected output is Z.",
    "intent": "brief intent phrase"
}}

Only output the JSON, nothing else."""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )

        import json
        result_text = response.content[0].text.strip()
        # Handle potential markdown code blocks
        if result_text.startswith('```'):
            result_text = result_text.split('```')[1]
            if result_text.startswith('json'):
                result_text = result_text[4:]
        result = json.loads(result_text)

        # Validate and sanitize
        import re
        project_name = re.sub(r'[^a-z0-9_]', '', result.get('project_name', 'chord').lower().replace(' ', '_').replace('-', '_'))[:30]
        if len(project_name) < 2:
            project_name = f"chord-{len(entries)}"

        return {
            "project_name": project_name,
            "title": result.get('title', 'Chord Project')[:60],
            "summary": result.get('summary', 'Implement the concepts from source notes.'),
            "intent": result.get('intent', 'project implementation')
        }

    except Exception as e:
        logger.error(f"Failed to generate chord summary: {e}")
        # Fallback
        if len(entries) == 1:
            title = entries[0]['title']
            slug = title.lower().replace(' ', '_')[:30]
            import re
            slug = re.sub(r'[^a-z0-9_]', '', slug)
            return {
                "project_name": slug or "single-note",
                "title": title,
                "summary": f"Implement the concept: {title}",
                "intent": entries[0].get('category', 'project')
            }
        return {
            "project_name": f"chord-{len(entries)}-notes",
            "title": f"Combined Chord ({len(entries)} notes)",
            "summary": "Implement the combined concepts from the source notes.",
            "intent": "multi-note project"
        }


@library_bp.route('/api/create-chord', methods=['POST'])
@login_required
def api_create_chord():
    """Create a Chord from one or more Notes.

    Uses Claude to generate a project title and summary from the note content.
    The agent receives both the summary (directive) and full note content (context).

    Request body:
    {
        "entry_ids": ["kb-abc123", "kb-def456"],
        "project_name": "optional-override"  // Optional - will be auto-generated
    }

    Response:
    {
        "status": "queued",
        "queue_id": "aq-xyz789",
        "project_name": "generated-name",
        "title": "Generated Title",
        "summary": "What the agent will do...",
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
    manual_project_name = data.get('project_name', '').strip()

    if not entry_ids:
        return jsonify({'error': 'entry_ids is required'}), 400

    try:
        legato_db = get_db()
        agents_db = get_agents_db()

        # Fetch all entries with full content
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

        # Generate title and summary using Claude
        chord_info = generate_chord_summary(entries)

        # Use manual override if provided
        if manual_project_name:
            project_name = re.sub(r'[^a-z0-9-]', '', manual_project_name.lower())[:30]
            if len(project_name) < 2:
                project_name = chord_info['project_name']
        else:
            project_name = chord_info['project_name']

        title = chord_info['title']
        summary = chord_info['summary']
        intent = chord_info['intent']

        # Build full context sections (complete content, not truncated)
        full_context_sections = []
        for entry in entries:
            full_context_sections.append(
                f"## Source Note: {entry['title']}\n"
                f"**Entry ID:** {entry['entry_id']}\n"
                f"**Category:** {entry.get('category', 'uncategorized')}\n\n"
                f"{entry['content'] or '(No content)'}\n\n"
                f"---"
            )

        # Build tasker body with summary directive + full context
        tasker_body = f"""## Tasker: {title}

### Summary & Directive
{summary}

### Full Source Context
The following is the complete content from {len(entries)} source note(s). Use this as reference material.

{chr(10).join(full_context_sections)}

### Acceptance Criteria
- [ ] Core functionality implemented as described in the summary
- [ ] All relevant concepts from source notes addressed
- [ ] Code is well-documented
- [ ] Tests cover main functionality

### Constraints
- Follow patterns in `copilot-instructions.md` if present
- Reference `SIGNAL.md` for project intent
- Keep PRs focused and reviewable

### References
{chr(10).join(f'- Source: `{e["entry_id"]}` - {e["title"]}' for e in entries)}

---
*Generated from {len(entries)} Library note(s) | Intent: {intent}*
"""

        # Build signal JSON
        signal_json = {
            "id": f"{project_name}.Chord",
            "type": "project",
            "source": "pit-library",
            "category": "chord",
            "title": title,
            "domain_tags": [],
            "intent": intent,
            "summary": summary,
            "key_phrases": [],
            "source_entries": [e['entry_id'] for e in entries],
            "path": f"{project_name}.Chord",
        }

        # Generate queue ID
        queue_id = f"aq-{secrets.token_hex(6)}"

        # Store all entry IDs as comma-separated in related_entry_id
        related_ids = ','.join(e['entry_id'] for e in entries)

        # Get user_id for multi-tenant isolation
        user_id = session.get('user', {}).get('user_id')

        agents_db.execute(
            """
            INSERT INTO agent_queue
            (queue_id, project_name, project_type, title, description,
             signal_json, tasker_body, source_transcript, related_entry_id, status, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                queue_id,
                project_name,
                'chord',
                title,
                summary,
                json.dumps(signal_json),
                tasker_body,
                f"library:chord:{len(entries)}",
                related_ids,
                user_id,
            )
        )
        agents_db.commit()

        logger.info(f"Queued chord: {queue_id} - {title} ({project_name}) from {len(entries)} notes")

        return jsonify({
            'status': 'queued',
            'queue_id': queue_id,
            'project_name': project_name,
            'title': title,
            'summary': summary,
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
        from .core import get_user_library_repo
        from .auth import get_user_installation_token
        from flask import session

        user_id = session.get('user', {}).get('user_id')
        token = get_user_installation_token(user_id, 'library') if user_id else None
        if not token:
            return jsonify({
                'status': 'error',
                'error': 'GitHub authorization required. Please re-authenticate.'
            }), 401

        repo = get_user_library_repo()

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


@library_bp.route('/api/create-note', methods=['POST'])
@login_required
def api_create_note():
    """Create a new note in the Library.

    Request body:
    {
        "title": "Note Title",
        "category": "concept",
        "content": "Note content in markdown..."
    }

    Response:
    {
        "status": "success",
        "entry_id": "kb-abc123",
        "file_path": "concepts/2026-01-10-note-title.md"
    }
    """
    import hashlib
    import re
    from datetime import datetime
    from flask import session
    from .rag.database import get_user_categories
    from .rag.github_service import create_file

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    title = data.get('title', '').strip()
    category = data.get('category', '').lower().strip()
    content = data.get('content', '').strip()

    if not title:
        return jsonify({'error': 'Title is required'}), 400

    if not category:
        return jsonify({'error': 'Category is required'}), 400

    # Validate category
    db = get_db()
    user_id = session.get('user', {}).get('user_id') or 'default'
    categories = get_user_categories(db, user_id)
    valid_categories = {c['name'] for c in categories}
    category_folders = {c['name']: c['folder_name'] for c in categories}

    if category not in valid_categories:
        return jsonify({
            'error': f'Invalid category. Must be one of: {", ".join(sorted(valid_categories))}'
        }), 400

    try:
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
domain_tags: []
key_phrases: []
---

"""
        full_content = frontmatter + content

        # Create file in GitHub
        from .core import get_user_library_repo
        from .auth import get_user_installation_token
        from flask import session

        user_id = session.get('user', {}).get('user_id')
        token = get_user_installation_token(user_id, 'library') if user_id else None
        if not token:
            return jsonify({'error': 'GitHub authorization required. Please re-authenticate.'}), 401

        repo = get_user_library_repo()

        create_file(
            repo=repo,
            path=file_path,
            content=full_content,
            message=f'Create note: {title}',
            token=token
        )

        # Insert into local database
        db.execute(
            """
            INSERT INTO knowledge_entries
            (entry_id, title, category, content, file_path, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (entry_id, title, category, content, file_path)
        )
        db.commit()

        logger.info(f"Created note: {entry_id} - {title} in {category}")

        return jsonify({
            'status': 'success',
            'entry_id': entry_id,
            'file_path': file_path,
        })

    except Exception as e:
        logger.error(f"Create note failed: {e}")
        return jsonify({'error': str(e)}), 500


@library_bp.route('/api/entry/<path:entry_id>/category', methods=['POST'])
@login_required
def api_update_category(entry_id: str):
    """Update an entry's category.

    This moves the file to the new category folder in GitHub and updates frontmatter.

    Request body:
    {
        "category": "concept"
    }

    Valid categories are loaded dynamically from user_categories table.

    Response:
    {
        "status": "success",
        "entry_id": "kb-abc123",
        "old_category": "glimmer",
        "new_category": "concept"
    }
    """
    import re
    from .rag.database import get_user_categories

    # Get categories dynamically from database
    db = get_db()
    user_id = session.get('user', {}).get('user_id') or 'default'
    categories = get_user_categories(db, user_id)
    valid_categories = {c['name'] for c in categories}
    category_folders = {c['name']: c['folder_name'] for c in categories}

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    new_category = data.get('category', '').lower().strip()
    if new_category not in valid_categories:
        return jsonify({
            'error': f'Invalid category. Must be one of: {", ".join(sorted(valid_categories))}'
        }), 400

    try:
        db = get_db()

        # Get current category and file path
        entry = db.execute(
            "SELECT category, file_path FROM knowledge_entries WHERE entry_id = ?",
            (entry_id,)
        ).fetchone()

        if not entry:
            return jsonify({'error': 'Entry not found'}), 404

        old_category = entry['category']
        old_file_path = entry['file_path']

        if old_category == new_category:
            return jsonify({
                'status': 'success',
                'message': 'Category unchanged',
                'entry_id': entry_id,
                'category': new_category
            })

        new_file_path = old_file_path
        github_updated = False

        # Move file in GitHub if file_path exists
        if old_file_path:
            try:
                from .rag.github_service import get_file_content, delete_file, create_file
                from .core import get_user_library_repo
                from .auth import get_user_installation_token

                user_id = session.get('user', {}).get('user_id')
                token = get_user_installation_token(user_id, 'library') if user_id else None
                repo = get_user_library_repo()

                # Get current content
                content = get_file_content(repo, old_file_path, token)
                if content:
                    # Update category in frontmatter
                    if content.startswith('---'):
                        parts = content.split('---', 2)
                        if len(parts) >= 3:
                            frontmatter = parts[1]
                            body = parts[2]

                            # Replace category line
                            new_frontmatter = re.sub(
                                r'^category:\s*.*$',
                                f'category: {new_category}',
                                frontmatter,
                                flags=re.MULTILINE
                            )

                            content = f'---{new_frontmatter}---{body}'

                    # Calculate new file path
                    # Old: glimmers/2024-01-10-something.md -> New: concepts/2024-01-10-something.md
                    filename = old_file_path.split('/')[-1]
                    new_folder = category_folders.get(new_category, f'{new_category}s')
                    new_file_path = f'{new_folder}/{filename}'

                    # Create file in new location
                    create_file(
                        repo=repo,
                        path=new_file_path,
                        content=content,
                        message=f'Move to {new_category}: {filename}',
                        token=token
                    )

                    # Delete from old location
                    delete_file(
                        repo=repo,
                        path=old_file_path,
                        message=f'Moved to {new_folder}/',
                        token=token
                    )

                    github_updated = True
                    logger.info(f"Moved {old_file_path} -> {new_file_path} in GitHub")

            except Exception as e:
                logger.warning(f"Could not move file in GitHub: {e}")
                # Continue anyway - DB will be updated

        # Update in database
        db.execute(
            "UPDATE knowledge_entries SET category = ?, file_path = ? WHERE entry_id = ?",
            (new_category, new_file_path, entry_id)
        )
        db.commit()

        logger.info(f"Changed category for {entry_id}: {old_category} -> {new_category}")

        return jsonify({
            'status': 'success',
            'entry_id': entry_id,
            'old_category': old_category,
            'new_category': new_category,
            'old_path': old_file_path,
            'new_path': new_file_path,
            'github_updated': github_updated
        })

    except Exception as e:
        logger.error(f"Update category failed: {e}")
        return jsonify({'error': str(e)}), 500


@library_bp.route('/api/entry/<path:entry_id>', methods=['DELETE'])
@login_required
def api_delete_entry(entry_id: str):
    """Delete an entry from the Library.

    Removes from:
    - GitHub (Legato.Library repo)
    - Local database
    - Embeddings

    Response:
    {
        "status": "success",
        "entry_id": "kb-abc123",
        "github_deleted": true
    }
    """
    try:
        db = get_db()

        # Get entry info
        entry = db.execute(
            "SELECT id, title, file_path, category FROM knowledge_entries WHERE entry_id = ?",
            (entry_id,)
        ).fetchone()

        if not entry:
            return jsonify({'error': 'Entry not found'}), 404

        entry_dict = dict(entry)
        file_path = entry_dict.get('file_path')
        github_deleted = False

        # Delete from GitHub if file_path exists
        if file_path:
            try:
                from .rag.github_service import delete_file
                from .core import get_user_library_repo
                from .auth import get_user_installation_token

                user_id = session.get('user', {}).get('user_id')
                token = get_user_installation_token(user_id, 'library') if user_id else None
                repo = get_user_library_repo()

                delete_file(
                    repo=repo,
                    path=file_path,
                    message=f"Delete: {entry_dict['title']}",
                    token=token
                )
                github_deleted = True
                logger.info(f"Deleted {file_path} from GitHub")

            except Exception as e:
                logger.warning(f"Could not delete from GitHub: {e}")
                # Continue anyway - will delete from DB

        # Delete embeddings
        db.execute(
            "DELETE FROM embeddings WHERE entry_id = ? AND entry_type = 'knowledge'",
            (entry_dict['id'],)
        )

        # Delete from database
        db.execute(
            "DELETE FROM knowledge_entries WHERE entry_id = ?",
            (entry_id,)
        )
        db.commit()

        # Delete any linked pending agents
        try:
            from .rag.database import init_agents_db
            agents_db = init_agents_db()
            # Match entry_id in related_entry_id (may be comma-separated for multi-note chords)
            agents_db.execute(
                """
                DELETE FROM agent_queue
                WHERE status = 'pending'
                AND (related_entry_id = ? OR related_entry_id LIKE ? OR related_entry_id LIKE ? OR related_entry_id LIKE ?)
                """,
                (entry_id, f"{entry_id},%", f"%,{entry_id},%", f"%,{entry_id}")
            )
            agents_db.commit()
            logger.info(f"Cleaned up any pending agents linked to {entry_id}")
        except Exception as e:
            logger.warning(f"Could not clean up linked agents: {e}")

        logger.info(f"Deleted entry {entry_id}: {entry_dict['title']}")

        return jsonify({
            'status': 'success',
            'entry_id': entry_id,
            'title': entry_dict['title'],
            'github_deleted': github_deleted
        })

    except Exception as e:
        logger.error(f"Delete entry failed: {e}")
        return jsonify({'error': str(e)}), 500


@library_bp.route('/graph')
@login_required
def graph_view():
    """Graph visualization of library entries by embedding similarity."""
    # Get categories for the legend
    categories = get_categories_with_counts()

    return render_template(
        'library_graph.html',
        categories=categories,
    )


@library_bp.route('/graph3d')
@login_required
def graph3d_view():
    """3D graph visualization of library entries by embedding similarity."""
    # Get categories for the legend
    categories = get_categories_with_counts()

    return render_template(
        'library_graph3d.html',
        categories=categories,
    )


@library_bp.route('/api/graph')
@login_required
def api_graph():
    """Get graph data for visualization.

    Returns nodes (entries with category info) and edges (embedding similarity).
    Categories are the primary organization for nodes.

    Query params:
    - threshold: Minimum similarity to create edge (default 0.3)
    - limit: Max entries to include (default 200)

    Response:
    {
        "nodes": [
            {"id": "kb-abc123", "title": "...", "category": "concept", "color": "#6366f1"},
            ...
        ],
        "edges": [
            {"source": "kb-abc123", "target": "kb-def456", "weight": 0.75},
            ...
        ],
        "categories": [
            {"name": "concept", "color": "#6366f1", "count": 10},
            ...
        ]
    }
    """
    import numpy as np
    from .rag.database import get_user_categories

    threshold = float(request.args.get('threshold', 0.3))
    limit = int(request.args.get('limit', 200))

    try:
        db = get_db()
        user_id = session.get('user', {}).get('user_id') or 'default'

        # Get user categories for colors
        user_cats = get_user_categories(db, user_id)
        category_colors = {c['name']: c.get('color', '#6366f1') for c in user_cats}

        # Get entries with embeddings (pick latest embedding per entry to avoid duplicates)
        rows = db.execute(
            """
            SELECT
                ke.entry_id,
                ke.title,
                ke.category,
                e.embedding
            FROM knowledge_entries ke
            INNER JOIN embeddings e ON e.entry_id = ke.id AND e.entry_type = 'knowledge'
            WHERE e.id = (
                SELECT e2.id FROM embeddings e2
                WHERE e2.entry_id = ke.id AND e2.entry_type = 'knowledge'
                ORDER BY e2.created_at DESC
                LIMIT 1
            )
            ORDER BY ke.created_at DESC
            LIMIT ?
            """,
            (limit,)
        ).fetchall()

        if not rows:
            return jsonify({
                'nodes': [],
                'edges': [],
                'categories': []
            })

        # Build nodes list
        nodes = []
        embeddings = []
        entry_ids = []

        for row in rows:
            entry_id = row['entry_id']
            category = row['category'] or 'uncategorized'
            color = category_colors.get(category, '#9ca3af')

            nodes.append({
                'id': entry_id,
                'title': row['title'],
                'category': category,
                'color': color,
            })

            # Decode embedding
            if row['embedding']:
                embedding = np.frombuffer(row['embedding'], dtype=np.float32)
                embeddings.append(embedding)
                entry_ids.append(entry_id)

        # Compute pairwise cosine similarities
        edges = []
        if len(embeddings) >= 2:
            # Stack embeddings into matrix
            matrix = np.vstack(embeddings)

            # Normalize for cosine similarity
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            norms[norms == 0] = 1  # Avoid division by zero
            normalized = matrix / norms

            # Compute similarity matrix
            similarity_matrix = np.dot(normalized, normalized.T)

            # Extract edges above threshold
            n = len(entry_ids)
            for i in range(n):
                for j in range(i + 1, n):
                    sim = float(similarity_matrix[i, j])
                    if sim >= threshold:
                        edges.append({
                            'source': entry_ids[i],
                            'target': entry_ids[j],
                            'weight': round(sim, 3),
                        })

        # Category summary
        category_counts = {}
        for node in nodes:
            cat = node['category']
            if cat not in category_counts:
                category_counts[cat] = {'name': cat, 'color': node['color'], 'count': 0}
            category_counts[cat]['count'] += 1

        return jsonify({
            'nodes': nodes,
            'edges': edges,
            'categories': list(category_counts.values()),
        })

    except Exception as e:
        logger.error(f"Graph API failed: {e}")
        return jsonify({'error': str(e)}), 500


@library_bp.route('/api/cleanup-orphans', methods=['POST'])
@login_required
def api_cleanup_orphans():
    """Find and reset notes with orphaned chord references.

    Checks notes with chord_status='active' against actual chord repos.
    If the chord repo no longer exists, resets the note's chord fields.

    Response:
    {
        "status": "success",
        "orphans_found": 2,
        "orphans_reset": ["kb-abc123", "kb-def456"]
    }
    """
    from .chords import fetch_chord_repos

    try:
        db = get_db()
        from .auth import get_user_installation_token
        from flask import session

        user_id = session.get('user', {}).get('user_id')
        token = get_user_installation_token(user_id, 'library') if user_id else None
        org = session.get('user', {}).get('username', 'bobbyhiddn')

        if not token:
            return jsonify({'error': 'GitHub authorization required. Please re-authenticate.'}), 401

        # Get all notes with active chord status
        notes_with_chords = db.execute(
            """
            SELECT entry_id, title, chord_repo, chord_status
            FROM knowledge_entries
            WHERE chord_status = 'active' AND chord_repo IS NOT NULL
            """
        ).fetchall()

        if not notes_with_chords:
            return jsonify({
                'status': 'success',
                'orphans_found': 0,
                'orphans_reset': [],
                'message': 'No notes with active chords found'
            })

        # Fetch all valid chord repos from GitHub
        valid_repos = fetch_chord_repos(token, org)
        valid_repo_names = {r['name'] for r in valid_repos}

        # Find orphaned notes
        orphans = []
        for note in notes_with_chords:
            chord_repo = note['chord_repo']
            # chord_repo might be full name like "bobbyhiddn/foo.Chord" or just "foo.Chord"
            repo_name = chord_repo.split('/')[-1] if '/' in chord_repo else chord_repo

            if repo_name not in valid_repo_names:
                orphans.append({
                    'entry_id': note['entry_id'],
                    'title': note['title'],
                    'chord_repo': chord_repo
                })

        # Reset orphaned notes
        reset_ids = []
        for orphan in orphans:
            db.execute(
                """
                UPDATE knowledge_entries
                SET chord_status = NULL, chord_repo = NULL, chord_id = NULL
                WHERE entry_id = ?
                """,
                (orphan['entry_id'],)
            )
            reset_ids.append(orphan['entry_id'])
            logger.info(f"Reset orphaned note: {orphan['entry_id']} (was linked to {orphan['chord_repo']})")

        db.commit()

        return jsonify({
            'status': 'success',
            'orphans_found': len(orphans),
            'orphans_reset': reset_ids,
            'details': orphans
        })

    except Exception as e:
        logger.error(f"Cleanup orphans failed: {e}")
        return jsonify({'error': str(e)}), 500
