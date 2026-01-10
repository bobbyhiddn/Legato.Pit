"""
Legato.Pit Core Application

Dashboard and Transcript Dropbox for the LEGATO system.
"""
import os
import logging
import secrets
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Flask, render_template, jsonify, request, redirect,
    url_for, session, flash
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

logger = logging.getLogger(__name__)


def create_app():
    """Application factory."""
    static_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
    template_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')

    app = Flask(
        __name__,
        static_folder=static_folder,
        template_folder=template_folder,
        static_url_path='/static'
    )

    # Apply proxy fix for Fly.io (trust X-Forwarded-* headers)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    # Security configuration
    is_production = os.getenv('FLASK_ENV') == 'production'
    app.config.update(
        SECRET_KEY=os.getenv('FLASK_SECRET_KEY', secrets.token_hex(32)),
        SESSION_COOKIE_SECURE=is_production,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
        PREFERRED_URL_SCHEME='https' if is_production else 'http',

        # GitHub OAuth (env vars use GH_ prefix to avoid GitHub's reserved GITHUB_ prefix)
        GITHUB_CLIENT_ID=os.getenv('GH_OAUTH_CLIENT_ID'),
        GITHUB_CLIENT_SECRET=os.getenv('GH_OAUTH_CLIENT_SECRET'),
        GITHUB_ALLOWED_USERS=os.getenv('GH_ALLOWED_USERS', '').split(','),

        # LEGATO configuration
        LEGATO_ORG=os.getenv('LEGATO_ORG', 'bobbyhiddn'),
        CONDUCT_REPO=os.getenv('CONDUCT_REPO', 'Legato.Conduct'),
        SYSTEM_PAT=os.getenv('SYSTEM_PAT'),

        # App metadata
        APP_NAME='Legato.Pit',
        APP_DESCRIPTION='Dashboard & Transcript Dropbox for LEGATO'
    )

    # Rate limiting
    limiter = Limiter(
        key_func=get_remote_address,
        app=app,
        default_limits=["200 per day", "50 per hour"],
        storage_uri="memory://"
    )

    # Store limiter on app for use in blueprints
    app.limiter = limiter

    # Register blueprints
    from .auth import auth_bp
    from .dashboard import dashboard_bp
    from .dropbox import dropbox_bp
    from .library import library_bp
    from .chat import chat_bp
    from .memory_api import memory_api_bp
    from .agents import agents_bp
    from .chords import chords_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(dropbox_bp)
    app.register_blueprint(library_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(memory_api_bp)
    app.register_blueprint(agents_bp)
    app.register_blueprint(chords_bp)

    # Initialize all databases on startup
    with app.app_context():
        from .rag.database import init_db, init_agents_db, init_chat_db
        init_db()        # legato.db - knowledge entries, embeddings
        init_agents_db() # agents.db - agent queue
        init_chat_db()   # chat.db - chat sessions/messages
        logger.info("All databases initialized (legato.db, agents.db, chat.db)")

    # Auto-sync library on startup (background thread)
    def startup_sync():
        """Sync library from GitHub on app startup."""
        import threading
        import time

        def sync_task():
            time.sleep(5)  # Wait for app to fully initialize
            try:
                with app.app_context():
                    from .rag.database import init_db
                    from .rag.library_sync import LibrarySync
                    from .rag.embedding_service import EmbeddingService
                    from .rag.openai_provider import OpenAIEmbeddingProvider

                    token = os.getenv('SYSTEM_PAT')
                    if not token:
                        logger.warning("SYSTEM_PAT not set, skipping library sync")
                        return

                    db = init_db()

                    # Clean up invalid/duplicate entries first
                    cleanup_count = 0
                    # Remove entries with invalid entry_ids
                    invalid = db.execute(
                        "SELECT id FROM knowledge_entries WHERE entry_id NOT LIKE 'kb-%' OR LENGTH(entry_id) != 11"
                    ).fetchall()
                    for row in invalid:
                        db.execute("DELETE FROM embeddings WHERE entry_id = ? AND entry_type = 'knowledge'", (row['id'],))
                        db.execute("DELETE FROM knowledge_entries WHERE id = ?", (row['id'],))
                        cleanup_count += 1
                    # Remove duplicates by file_path
                    dups = db.execute("""
                        SELECT id FROM knowledge_entries
                        WHERE file_path IS NOT NULL AND id NOT IN (
                            SELECT MAX(id) FROM knowledge_entries WHERE file_path IS NOT NULL GROUP BY file_path
                        )
                    """).fetchall()
                    for row in dups:
                        db.execute("DELETE FROM embeddings WHERE entry_id = ? AND entry_type = 'knowledge'", (row['id'],))
                        db.execute("DELETE FROM knowledge_entries WHERE id = ?", (row['id'],))
                        cleanup_count += 1
                    if cleanup_count > 0:
                        db.commit()
                        logger.info(f"Cleaned up {cleanup_count} invalid/duplicate entries")

                    # Create embedding service if OpenAI key available
                    embedding_service = None
                    if os.getenv('OPENAI_API_KEY'):
                        try:
                            provider = OpenAIEmbeddingProvider()
                            embedding_service = EmbeddingService(provider, db)
                        except Exception as e:
                            logger.warning(f"Could not create embedding service: {e}")

                    sync = LibrarySync(db, embedding_service)
                    stats = sync.sync_from_github('bobbyhiddn/Legato.Library', token=token)
                    logger.info(f"Startup library sync complete: {stats}")
            except Exception as e:
                logger.error(f"Startup library sync failed: {e}")

        thread = threading.Thread(target=sync_task, daemon=True)
        thread.start()
        logger.info("Started background library sync")

    startup_sync()

    # Context processor for templates
    @app.context_processor
    def inject_globals():
        return {
            'now': datetime.now(),
            'app_name': app.config['APP_NAME'],
            'app_description': app.config['APP_DESCRIPTION'],
            'user': session.get('user'),
            'is_authenticated': 'user' in session
        }

    # Root redirect
    @app.route('/')
    def index():
        if 'user' in session:
            return redirect(url_for('dashboard.index'))
        return redirect(url_for('auth.login'))

    # Health check (no auth required)
    @app.route('/health')
    def health():
        return jsonify({
            'status': 'healthy',
            'timestamp': datetime.now().isoformat(),
            'app': app.config['APP_NAME']
        })

    # Error handlers
    @app.errorhandler(404)
    def not_found_error(error):
        return render_template('error.html', title="Not Found", message="Page not found"), 404

    @app.errorhandler(500)
    def internal_error(error):
        return render_template('error.html', title="Server Error", message="An error occurred"), 500

    @app.errorhandler(429)
    def ratelimit_error(error):
        return render_template('error.html', title="Rate Limited", message="Too many requests. Please wait."), 429

    logger.info("Legato.Pit application initialized")
    return app


def login_required(f):
    """Decorator to require authentication."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function
