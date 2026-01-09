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

        # GitHub OAuth
        GITHUB_CLIENT_ID=os.getenv('GITHUB_CLIENT_ID'),
        GITHUB_CLIENT_SECRET=os.getenv('GITHUB_CLIENT_SECRET'),
        GITHUB_ALLOWED_USERS=os.getenv('GITHUB_ALLOWED_USERS', '').split(','),

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

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(dropbox_bp)
    app.register_blueprint(library_bp)
    app.register_blueprint(chat_bp)
    app.register_blueprint(memory_api_bp)

    # Initialize RAG database on startup
    with app.app_context():
        from .rag.database import init_db
        init_db()
        logger.info("RAG database initialized")

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
