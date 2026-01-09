"""
Memory API - RAG Endpoints for Pipeline Integration

Provides REST API for:
- Correlation checking (before extraction)
- Entry registration (after extraction)
- Similarity search
"""

import os
import logging
from functools import wraps

from flask import Blueprint, request, jsonify, current_app, g

logger = logging.getLogger(__name__)

memory_api_bp = Blueprint('memory_api', __name__, url_prefix='/memory/api')


def require_api_token(f):
    """Decorator to require Bearer token authentication."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')

        if not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Missing or invalid Authorization header'}), 401

        token = auth_header[7:]  # Remove 'Bearer ' prefix
        expected_token = current_app.config.get('SYSTEM_PAT')

        if not expected_token or token != expected_token:
            return jsonify({'error': 'Invalid token'}), 403

        return f(*args, **kwargs)
    return decorated


def get_embedding_service():
    """Get or create the embedding service."""
    if 'embedding_service' not in g:
        from .rag.database import init_db
        from .rag.embedding_service import EmbeddingService
        from .rag.openai_provider import OpenAIEmbeddingProvider

        # Initialize database if needed
        if 'db_conn' not in g:
            g.db_conn = init_db()

        # Create provider (prefer OpenAI for API consistency)
        try:
            provider = OpenAIEmbeddingProvider()
        except ValueError:
            # Fall back to Ollama if OpenAI not configured
            from .rag.ollama_provider import OllamaEmbeddingProvider
            provider = OllamaEmbeddingProvider()

        g.embedding_service = EmbeddingService(provider, g.db_conn)

    return g.embedding_service


@memory_api_bp.route('/health', methods=['GET'])
def health():
    """Health check for the memory API."""
    return jsonify({
        'status': 'healthy',
        'service': 'memory_api',
    })


@memory_api_bp.route('/correlate', methods=['POST'])
@require_api_token
def correlate():
    """Check if similar content already exists.

    Request body:
    {
        "title": "Entry title",
        "content": "Entry content",
        "key_phrases": ["optional", "phrases"]  # Optional
    }

    Response:
    {
        "action": "CREATE" | "SUGGEST" | "SKIP",
        "score": 0.85,
        "matches": [
            {"entry_id": "kb-001", "title": "...", "similarity": 0.85}
        ]
    }
    """
    data = request.get_json()

    if not data:
        return jsonify({'error': 'JSON body required'}), 400

    title = data.get('title', '')
    content = data.get('content', '')
    key_phrases = data.get('key_phrases', [])

    if not title and not content:
        return jsonify({'error': 'title or content required'}), 400

    # Include key phrases in content for better matching
    if key_phrases:
        content = f"{content}\n\nKey phrases: {', '.join(key_phrases)}"

    try:
        service = get_embedding_service()
        result = service.correlate(title, content)
        logger.info(f"Correlation check: {title[:50]}... -> {result['action']} ({result['score']:.2f})")
        return jsonify(result)

    except Exception as e:
        logger.error(f"Correlation failed: {e}")
        return jsonify({'error': str(e)}), 500


@memory_api_bp.route('/register', methods=['POST'])
@require_api_token
def register():
    """Register a new knowledge entry.

    Request body:
    {
        "entry_id": "kb-001",
        "title": "Entry title",
        "category": "concepts",
        "content": "Entry content",
        "source_thread": "thread-001",  # Optional
        "source_transcript": "transcript.txt"  # Optional
    }

    Response:
    {
        "success": true,
        "id": 1,
        "embedding_generated": true
    }
    """
    data = request.get_json()

    if not data:
        return jsonify({'error': 'JSON body required'}), 400

    required = ['entry_id', 'title', 'content']
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({'error': f'Missing required fields: {missing}'}), 400

    try:
        service = get_embedding_service()
        conn = service.conn

        # Insert entry
        cursor = conn.execute(
            """
            INSERT INTO knowledge_entries
            (entry_id, title, category, content, source_thread, source_transcript)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(entry_id) DO UPDATE SET
                title = excluded.title,
                category = excluded.category,
                content = excluded.content,
                source_thread = excluded.source_thread,
                source_transcript = excluded.source_transcript,
                updated_at = CURRENT_TIMESTAMP
            RETURNING id
            """,
            (
                data['entry_id'],
                data['title'],
                data.get('category', 'general'),
                data['content'],
                data.get('source_thread'),
                data.get('source_transcript'),
            ),
        )
        row = cursor.fetchone()
        entry_db_id = row[0]
        conn.commit()

        # Generate embedding
        text = f"Title: {data['title']}\n\nContent: {data['content']}"
        embedding = service.generate_and_store(entry_db_id, 'knowledge', text)

        logger.info(f"Registered entry: {data['entry_id']}")

        return jsonify({
            'success': True,
            'id': entry_db_id,
            'embedding_generated': embedding is not None,
        })

    except Exception as e:
        logger.error(f"Registration failed: {e}")
        return jsonify({'error': str(e)}), 500


@memory_api_bp.route('/search', methods=['GET'])
@require_api_token
def search():
    """Search for similar entries.

    Query params:
    - q: Search query (required)
    - limit: Max results (default 10)
    - threshold: Min similarity (default 0.4)
    - type: Entry type (default 'knowledge')

    Response:
    {
        "results": [
            {
                "entry_id": "kb-001",
                "title": "...",
                "category": "concepts",
                "similarity": 0.85,
                "snippet": "First 200 chars..."
            }
        ]
    }
    """
    query = request.args.get('q', '')
    if not query:
        return jsonify({'error': 'q parameter required'}), 400

    limit = int(request.args.get('limit', 10))
    threshold = float(request.args.get('threshold', 0.4))
    entry_type = request.args.get('type', 'knowledge')

    try:
        service = get_embedding_service()
        results = service.find_similar(
            query_text=query,
            entry_type=entry_type,
            limit=limit,
            threshold=threshold,
        )

        # Format results
        formatted = [
            {
                'entry_id': r['entry_id'],
                'title': r['title'],
                'category': r.get('category'),
                'similarity': round(r['similarity'], 3),
                'snippet': (r.get('content', '')[:200] + '...') if r.get('content') else None,
            }
            for r in results
        ]

        return jsonify({'results': formatted})

    except Exception as e:
        logger.error(f"Search failed: {e}")
        return jsonify({'error': str(e)}), 500


@memory_api_bp.route('/stats', methods=['GET'])
@require_api_token
def stats():
    """Get knowledge base statistics.

    Response:
    {
        "knowledge_entries": 42,
        "project_entries": 5,
        "embeddings": 42,
        "provider": "openai:text-embedding-3-small"
    }
    """
    try:
        from .rag.context_builder import ContextBuilder

        service = get_embedding_service()
        builder = ContextBuilder(service)

        return jsonify(builder.get_stats())

    except Exception as e:
        logger.error(f"Stats failed: {e}")
        return jsonify({'error': str(e)}), 500


@memory_api_bp.route('/sync', methods=['POST'])
@require_api_token
def trigger_sync():
    """Trigger a sync from Library repository.

    Request body (optional):
    {
        "clear": true  // Clear all entries before sync (fixes duplicates)
    }
    """
    from .rag.database import init_db
    from .rag.library_sync import LibrarySync
    from .rag.embedding_service import EmbeddingService
    from .rag.openai_provider import OpenAIEmbeddingProvider

    data = request.get_json() or {}
    clear_first = data.get('clear', False)

    try:
        db = init_db()

        # Clear existing entries if requested
        if clear_first:
            db.execute("DELETE FROM embeddings WHERE entry_type = 'knowledge'")
            db.execute("DELETE FROM knowledge_entries")
            db.commit()
            logger.info("Cleared knowledge entries before sync")

        # Create embedding service if possible
        embedding_service = None
        if os.environ.get('OPENAI_API_KEY'):
            try:
                provider = OpenAIEmbeddingProvider()
                embedding_service = EmbeddingService(provider, db)
            except Exception:
                pass

        sync = LibrarySync(db, embedding_service)
        token = os.environ.get('SYSTEM_PAT')
        stats = sync.sync_from_github('bobbyhiddn/Legato.Library', token=token)

        return jsonify({
            'status': 'success',
            'stats': stats,
        })
    except Exception as e:
        logger.error(f"Sync failed: {e}")
        return jsonify({'error': str(e)}), 500


@memory_api_bp.route('/pipeline/status', methods=['POST'])
@require_api_token
def pipeline_status():
    """Update pipeline status.

    Called by Conduct workflows to report progress.

    Request body:
    {
        "run_id": "12345",
        "stage": "parse" | "classify" | "process-knowledge" | "process-projects" | "complete",
        "status": "started" | "success" | "failed",
        "details": {
            "thread_count": 5,
            "knowledge_count": 3,
            ...
        }
    }

    Response:
    {
        "success": true,
        "message": "Status updated"
    }
    """
    data = request.get_json()

    if not data:
        return jsonify({'error': 'JSON body required'}), 400

    required = ['run_id', 'stage', 'status']
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({'error': f'Missing required fields: {missing}'}), 400

    try:
        from .rag.database import init_db
        import json

        db = init_db()

        # Store pipeline status
        db.execute(
            """
            INSERT INTO pipeline_runs (run_id, stage, status, details, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(run_id, stage) DO UPDATE SET
                status = excluded.status,
                details = excluded.details,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                data['run_id'],
                data['stage'],
                data['status'],
                json.dumps(data.get('details', {})),
            ),
        )
        db.commit()

        logger.info(f"Pipeline {data['run_id']} stage {data['stage']}: {data['status']}")

        return jsonify({
            'success': True,
            'message': 'Status updated',
        })

    except Exception as e:
        logger.error(f"Pipeline status update failed: {e}")
        return jsonify({'error': str(e)}), 500


@memory_api_bp.route('/pipeline/status/<run_id>', methods=['GET'])
@require_api_token
def get_pipeline_status(run_id: str):
    """Get pipeline status for a run.

    Response:
    {
        "run_id": "12345",
        "stages": [
            {"stage": "parse", "status": "success", "details": {...}, "updated_at": "..."},
            {"stage": "classify", "status": "running", ...}
        ]
    }
    """
    try:
        from .rag.database import init_db
        import json

        db = init_db()

        rows = db.execute(
            """
            SELECT stage, status, details, updated_at
            FROM pipeline_runs
            WHERE run_id = ?
            ORDER BY updated_at
            """,
            (run_id,),
        ).fetchall()

        stages = [
            {
                'stage': r['stage'],
                'status': r['status'],
                'details': json.loads(r['details']) if r['details'] else {},
                'updated_at': r['updated_at'],
            }
            for r in rows
        ]

        return jsonify({
            'run_id': run_id,
            'stages': stages,
        })

    except Exception as e:
        logger.error(f"Get pipeline status failed: {e}")
        return jsonify({'error': str(e)}), 500
