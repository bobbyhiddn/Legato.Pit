"""
GitHub OAuth Authentication for Legato.Pit

Supports two authentication modes:
1. Legacy OAuth App (single-tenant, allowlist-based)
2. GitHub App (multi-tenant, installation-based)

Security measures:
- State parameter to prevent CSRF attacks
- User allowlist enforcement (legacy mode)
- Installation-scoped tokens (GitHub App mode)
- Per-user encryption for stored tokens
- Session fixation protection
"""
import os
import secrets
import logging
from datetime import datetime
from urllib.parse import urlencode
from typing import Optional

import requests
from flask import (
    Blueprint, redirect, url_for, session, request,
    current_app, flash, render_template, g
)

logger = logging.getLogger(__name__)

auth_bp = Blueprint('auth', __name__, url_prefix='/auth')

# GitHub OAuth endpoints
GITHUB_AUTHORIZE_URL = 'https://github.com/login/oauth/authorize'
GITHUB_TOKEN_URL = 'https://github.com/login/oauth/access_token'
GITHUB_USER_URL = 'https://api.github.com/user'

# GitHub App endpoints
GITHUB_APP_AUTHORIZE_URL = 'https://github.com/login/oauth/authorize'
GITHUB_APP_INSTALL_URL = 'https://github.com/apps/{app_slug}/installations/new'


@auth_bp.route('/login')
def login():
    """Display login page or initiate OAuth flow."""
    if 'user' in session:
        return redirect(url_for('dashboard.index'))

    # Check deployment mode and auth configuration
    is_multi_tenant = current_app.config.get('LEGATO_MODE') == 'multi-tenant'
    github_app_configured = is_multi_tenant and bool(current_app.config.get('GITHUB_APP_CLIENT_ID'))
    oauth_configured = bool(current_app.config.get('GITHUB_CLIENT_ID'))

    if not github_app_configured and not oauth_configured:
        flash('GitHub authentication not configured. Contact administrator.', 'error')

    return render_template('login.html',
                           github_app_configured=github_app_configured,
                           oauth_configured=oauth_configured)


@auth_bp.route('/github')
def github_login():
    """Initiate GitHub OAuth flow."""
    client_id = current_app.config.get('GITHUB_CLIENT_ID')

    if not client_id:
        flash('GitHub OAuth not configured.', 'error')
        return redirect(url_for('auth.login'))

    # Generate and store state for CSRF protection
    state = secrets.token_urlsafe(32)
    session['oauth_state'] = state

    # Build authorization URL
    params = {
        'client_id': client_id,
        'redirect_uri': url_for('auth.github_callback', _external=True),
        'scope': 'read:user',
        'state': state
    }

    auth_url = f"{GITHUB_AUTHORIZE_URL}?{urlencode(params)}"
    logger.info(f"Redirecting to GitHub OAuth: {auth_url}")

    return redirect(auth_url)


@auth_bp.route('/github/callback')
def github_callback():
    """Handle GitHub OAuth callback."""
    # Check if this is an MCP OAuth flow (shares callback URL with web login)
    if 'mcp_github_state' in session:
        from .oauth_server import handle_mcp_github_callback
        return handle_mcp_github_callback()

    # Verify state to prevent CSRF
    state = request.args.get('state')
    stored_state = session.pop('oauth_state', None)

    if not state or state != stored_state:
        logger.warning(f"OAuth state mismatch: received={state}, expected={stored_state}")
        flash('Authentication failed: Invalid state. Please try again.', 'error')
        return redirect(url_for('auth.login'))

    # Check for errors from GitHub
    error = request.args.get('error')
    if error:
        error_desc = request.args.get('error_description', 'Unknown error')
        logger.warning(f"GitHub OAuth error: {error} - {error_desc}")
        flash(f'GitHub authentication failed: {error_desc}', 'error')
        return redirect(url_for('auth.login'))

    # Get authorization code
    code = request.args.get('code')
    if not code:
        flash('Authentication failed: No authorization code received.', 'error')
        return redirect(url_for('auth.login'))

    # Exchange code for access token
    token_data = {
        'client_id': current_app.config['GITHUB_CLIENT_ID'],
        'client_secret': current_app.config['GITHUB_CLIENT_SECRET'],
        'code': code,
        'redirect_uri': url_for('auth.github_callback', _external=True)
    }

    try:
        token_response = requests.post(
            GITHUB_TOKEN_URL,
            data=token_data,
            headers={'Accept': 'application/json'},
            timeout=10
        )
        token_response.raise_for_status()
        token_json = token_response.json()
    except requests.RequestException as e:
        logger.error(f"Failed to exchange OAuth code: {e}")
        flash('Authentication failed: Could not verify with GitHub.', 'error')
        return redirect(url_for('auth.login'))

    access_token = token_json.get('access_token')
    if not access_token:
        error = token_json.get('error_description', 'No access token received')
        logger.warning(f"No access token in response: {token_json}")
        flash(f'Authentication failed: {error}', 'error')
        return redirect(url_for('auth.login'))

    # Fetch user info
    try:
        user_response = requests.get(
            GITHUB_USER_URL,
            headers={
                'Authorization': f'Bearer {access_token}',
                'Accept': 'application/vnd.github+json'
            },
            timeout=10
        )
        user_response.raise_for_status()
        user_data = user_response.json()
    except requests.RequestException as e:
        logger.error(f"Failed to fetch user info: {e}")
        flash('Authentication failed: Could not fetch user info.', 'error')
        return redirect(url_for('auth.login'))

    username = user_data.get('login')

    # Verify user is in allowlist
    allowed_users = current_app.config.get('GITHUB_ALLOWED_USERS', [])
    allowed_users = [u.strip() for u in allowed_users if u.strip()]

    if allowed_users and username not in allowed_users:
        logger.warning(f"Unauthorized user attempted login: {username}")
        flash('Access denied: You are not authorized to use this application.', 'error')
        return redirect(url_for('auth.login'))

    # Session fixation protection: regenerate session
    session.clear()

    # Store user info in session
    session['user'] = {
        'username': username,
        'name': user_data.get('name') or username,
        'avatar_url': user_data.get('avatar_url'),
        'github_id': user_data.get('id')
    }
    session['github_token'] = access_token
    session.permanent = True

    logger.info(f"User logged in: {username}")
    flash(f'Welcome, {session["user"]["name"]}!', 'success')

    return redirect(url_for('dashboard.index'))


@auth_bp.route('/logout')
def logout():
    """Log out the current user."""
    username = session.get('user', {}).get('username', 'unknown')
    session.clear()
    logger.info(f"User logged out: {username}")
    flash('You have been logged out.', 'info')
    return redirect(url_for('auth.login'))


# =============================================================================
# GitHub App Multi-Tenant Authentication
# =============================================================================

def _get_db():
    """Get database connection from app context."""
    from .rag.database import get_db
    return get_db()


def _get_or_create_user(github_id: int, github_login: str, email: Optional[str] = None,
                        name: Optional[str] = None, avatar_url: Optional[str] = None) -> dict:
    """Get existing user or create a new one.

    Args:
        github_id: GitHub user ID
        github_login: GitHub username
        email: User's email (optional)
        name: Display name (optional)
        avatar_url: Profile picture URL (optional)

    Returns:
        User dict with user_id and other fields
    """
    import uuid
    db = _get_db()

    # Check for existing user
    row = db.execute(
        "SELECT * FROM users WHERE github_id = ?",
        (github_id,)
    ).fetchone()

    if row:
        # Update login info
        db.execute(
            """
            UPDATE users SET github_login = ?, email = COALESCE(?, email),
                           updated_at = CURRENT_TIMESTAMP
            WHERE github_id = ?
            """,
            (github_login, email, github_id)
        )
        db.commit()
        return dict(row)

    # Create new user
    user_id = str(uuid.uuid4())
    db.execute(
        """
        INSERT INTO users (user_id, github_id, github_login, email, tier, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'free', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (user_id, github_id, github_login, email)
    )
    db.commit()

    logger.info(f"Created new user: {github_login} ({user_id})")

    return {
        'user_id': user_id,
        'github_id': github_id,
        'github_login': github_login,
        'email': email,
        'tier': 'free',
    }


def _store_installation(user_id: str, installation_id: int, installation_data: dict):
    """Store or update a GitHub App installation.

    Args:
        user_id: The user's ID
        installation_id: GitHub installation ID
        installation_data: Full installation data from GitHub
    """
    from .crypto import encrypt_for_user

    db = _get_db()
    account = installation_data.get('account', {})

    # Check if installation exists
    existing = db.execute(
        "SELECT id FROM github_app_installations WHERE installation_id = ?",
        (installation_id,)
    ).fetchone()

    if existing:
        db.execute(
            """
            UPDATE github_app_installations
            SET user_id = ?, account_login = ?, account_type = ?,
                permissions = ?, updated_at = CURRENT_TIMESTAMP
            WHERE installation_id = ?
            """,
            (
                user_id,
                account.get('login'),
                account.get('type'),
                str(installation_data.get('permissions', {})),
                installation_id
            )
        )
    else:
        db.execute(
            """
            INSERT INTO github_app_installations
            (installation_id, user_id, account_login, account_type, permissions, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                installation_id,
                user_id,
                account.get('login'),
                account.get('type'),
                str(installation_data.get('permissions', {}))
            )
        )

    db.commit()
    logger.info(f"Stored installation {installation_id} for user {user_id}")


def _log_audit(user_id: str, action: str, resource_type: str,
               resource_id: Optional[str] = None, details: Optional[str] = None):
    """Log an audit event.

    Args:
        user_id: The user performing the action
        action: Action type (login, logout, install, etc.)
        resource_type: Type of resource affected
        resource_id: ID of the resource (optional)
        details: Additional details as JSON string (optional)
    """
    db = _get_db()
    db.execute(
        """
        INSERT INTO audit_log (user_id, action, resource_type, resource_id, details, ip_address, created_at)
        VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (user_id, action, resource_type, resource_id, details, request.remote_addr)
    )
    db.commit()


@auth_bp.route('/app/login')
def github_app_login():
    """Initiate GitHub App OAuth flow.

    This uses the GitHub App's OAuth credentials (not the legacy OAuth App).
    Users are authenticated and can then install the app on their repos.
    """
    client_id = current_app.config.get('GITHUB_APP_CLIENT_ID')

    if not client_id:
        flash('GitHub App not configured. Using legacy authentication.', 'warning')
        return redirect(url_for('auth.github_login'))

    # Generate and store state for CSRF protection
    state = secrets.token_urlsafe(32)
    session['app_oauth_state'] = state

    # Build authorization URL with email scope for user identification
    params = {
        'client_id': client_id,
        'redirect_uri': url_for('auth.github_app_callback', _external=True),
        'scope': 'read:user user:email',
        'state': state
    }

    auth_url = f"{GITHUB_APP_AUTHORIZE_URL}?{urlencode(params)}"
    logger.info(f"Redirecting to GitHub App OAuth: {auth_url}")

    return redirect(auth_url)


@auth_bp.route('/app/callback')
def github_app_callback():
    """Handle GitHub App OAuth callback.

    After user authorizes, we:
    1. Exchange code for user access token
    2. Fetch user info
    3. Create/update user in database
    4. Check for existing installations
    5. Redirect to setup or dashboard
    """
    from .github_app import exchange_code_for_user_token, get_user_info, get_user_emails

    # Verify state to prevent CSRF
    state = request.args.get('state')
    stored_state = session.pop('app_oauth_state', None)

    if not state or state != stored_state:
        logger.warning(f"App OAuth state mismatch")
        flash('Authentication failed: Invalid state. Please try again.', 'error')
        return redirect(url_for('auth.login'))

    # Check for errors
    error = request.args.get('error')
    if error:
        error_desc = request.args.get('error_description', 'Unknown error')
        logger.warning(f"GitHub App OAuth error: {error} - {error_desc}")
        flash(f'Authentication failed: {error_desc}', 'error')
        return redirect(url_for('auth.login'))

    # Get authorization code
    code = request.args.get('code')
    if not code:
        flash('Authentication failed: No authorization code received.', 'error')
        return redirect(url_for('auth.login'))

    try:
        # Exchange code for tokens
        token_data = exchange_code_for_user_token(code)
        access_token = token_data.get('access_token')
        refresh_token = token_data.get('refresh_token')

        if not access_token:
            raise ValueError("No access token in response")

        # Fetch user info
        user_info = get_user_info(access_token)
        github_id = user_info.get('id')
        github_login = user_info.get('login')
        name = user_info.get('name')
        avatar_url = user_info.get('avatar_url')

        # Fetch primary email
        email = None
        try:
            emails = get_user_emails(access_token)
            for e in emails:
                if e.get('primary') and e.get('verified'):
                    email = e.get('email')
                    break
        except Exception as e:
            logger.warning(f"Could not fetch user emails: {e}")

        # Create or update user in database
        user = _get_or_create_user(github_id, github_login, email, name, avatar_url)

        # Store refresh token (encrypted)
        if refresh_token:
            from .crypto import encrypt_for_user
            db = _get_db()
            encrypted_refresh = encrypt_for_user(user['user_id'], refresh_token)
            db.execute(
                "UPDATE users SET refresh_token_encrypted = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
                (encrypted_refresh, user['user_id'])
            )
            db.commit()

        # Session fixation protection
        session.clear()

        # Store user info in session
        session['user'] = {
            'user_id': user['user_id'],
            'username': github_login,
            'name': name or github_login,
            'avatar_url': avatar_url,
            'github_id': github_id,
            'tier': user.get('tier', 'free'),
            'auth_mode': 'github_app'
        }
        session['github_token'] = access_token
        session.permanent = True

        # Log the login
        _log_audit(user['user_id'], 'login', 'user', user['user_id'], '{"method": "github_app"}')

        logger.info(f"GitHub App user logged in: {github_login}")

        # Check if user has any installations
        db = _get_db()
        installations = db.execute(
            "SELECT COUNT(*) as count FROM github_app_installations WHERE user_id = ?",
            (user['user_id'],)
        ).fetchone()

        if installations and installations['count'] > 0:
            flash(f'Welcome back, {name or github_login}!', 'success')
            return redirect(url_for('dashboard.index'))
        else:
            # First time user - redirect to setup
            flash(f'Welcome, {name or github_login}! Let\'s set up your Legato installation.', 'success')
            return redirect(url_for('auth.setup'))

    except Exception as e:
        logger.error(f"GitHub App OAuth failed: {e}")
        flash('Authentication failed. Please try again.', 'error')
        return redirect(url_for('auth.login'))


@auth_bp.route('/app/install')
def github_app_install():
    """Redirect user to install the GitHub App on their account/repos.

    This is called after login when user needs to grant repo access.
    """
    app_slug = current_app.config.get('GITHUB_APP_SLUG', 'legato-studio')

    # If we have specific repos suggested, we could add them as query params
    # For now, let user choose during installation
    install_url = f"https://github.com/apps/{app_slug}/installations/new"

    logger.info(f"Redirecting to GitHub App installation: {install_url}")
    return redirect(install_url)


@auth_bp.route('/app/installed')
def github_app_installed():
    """Handle post-installation callback from GitHub.

    GitHub redirects here after user installs the app.
    Query params include installation_id and setup_action.
    """
    from .github_app import get_installation_access_token

    installation_id = request.args.get('installation_id')
    setup_action = request.args.get('setup_action')

    if not installation_id:
        flash('Installation failed: No installation ID received.', 'error')
        return redirect(url_for('auth.setup'))

    # User must be logged in
    if 'user' not in session:
        # Store installation ID and redirect to login
        session['pending_installation_id'] = installation_id
        flash('Please log in to complete the installation.', 'info')
        return redirect(url_for('auth.github_app_login'))

    user = session['user']
    user_id = user.get('user_id')

    if not user_id:
        flash('Session error. Please log in again.', 'error')
        return redirect(url_for('auth.login'))

    try:
        installation_id = int(installation_id)

        # Fetch installation details
        from .github_app import get_app_installations
        installations = get_app_installations()

        installation_data = None
        for inst in installations:
            if inst.get('id') == installation_id:
                installation_data = inst
                break

        if not installation_data:
            flash('Could not verify installation. Please try again.', 'error')
            return redirect(url_for('auth.setup'))

        # Store installation in database
        _store_installation(user_id, installation_id, installation_data)

        # Log the installation
        account_login = installation_data.get('account', {}).get('login', 'unknown')
        _log_audit(user_id, 'install', 'installation', str(installation_id),
                   f'{{"account": "{account_login}"}}')

        # Verify we can get a token
        token_data = get_installation_access_token(installation_id)

        flash(f'Successfully installed Legato on {account_login}!', 'success')
        logger.info(f"Installation {installation_id} completed for user {user_id}")

        return redirect(url_for('auth.setup'))

    except Exception as e:
        logger.error(f"Failed to complete installation: {e}")
        flash('Failed to complete installation. Please try again.', 'error')
        return redirect(url_for('auth.setup'))


@auth_bp.route('/setup')
def setup():
    """Setup page for new users or users needing to configure repos.

    Shows:
    - Current installations and their repos
    - Option to install on more repos
    - API key configuration (for BYK tier)
    - Repo designation (Library, Conduct)
    """
    if 'user' not in session:
        return redirect(url_for('auth.login'))

    user = session['user']
    user_id = user.get('user_id')

    # For legacy auth users, redirect to dashboard
    if user.get('auth_mode') != 'github_app':
        return redirect(url_for('dashboard.index'))

    db = _get_db()

    # Get user's installations
    installations = db.execute(
        """
        SELECT installation_id, account_login, account_type, permissions, created_at
        FROM github_app_installations
        WHERE user_id = ?
        ORDER BY created_at DESC
        """,
        (user_id,)
    ).fetchall()

    # Get user's designated repos
    repos = db.execute(
        """
        SELECT repo_type, repo_full_name, installation_id
        FROM user_repos
        WHERE user_id = ?
        """,
        (user_id,)
    ).fetchall()

    # Get user's API keys (just hints, not actual keys)
    api_keys = db.execute(
        """
        SELECT provider, key_hint, created_at
        FROM user_api_keys
        WHERE user_id = ?
        """,
        (user_id,)
    ).fetchall()

    # Get full user record for tier info
    user_record = db.execute(
        "SELECT tier FROM users WHERE user_id = ?",
        (user_id,)
    ).fetchone()

    return render_template('setup.html',
                           user=user,
                           tier=user_record['tier'] if user_record else 'free',
                           installations=[dict(i) for i in installations],
                           repos=[dict(r) for r in repos],
                           api_keys=[dict(k) for k in api_keys])


@auth_bp.route('/setup/repo', methods=['POST'])
def setup_repo():
    """Designate a repository for Library or Conduct.

    POST params:
    - repo_type: 'library' or 'conduct'
    - repo_full_name: 'owner/repo'
    - installation_id: The installation that has access
    """
    if 'user' not in session:
        return redirect(url_for('auth.login'))

    user = session['user']
    user_id = user.get('user_id')

    repo_type = request.form.get('repo_type')
    repo_full_name = request.form.get('repo_full_name')
    installation_id = request.form.get('installation_id')

    if repo_type not in ('library', 'conduct'):
        flash('Invalid repository type.', 'error')
        return redirect(url_for('auth.setup'))

    if not repo_full_name or not installation_id:
        flash('Repository name and installation are required.', 'error')
        return redirect(url_for('auth.setup'))

    try:
        db = _get_db()

        # Verify installation belongs to user
        inst = db.execute(
            "SELECT installation_id FROM github_app_installations WHERE installation_id = ? AND user_id = ?",
            (installation_id, user_id)
        ).fetchone()

        if not inst:
            flash('Invalid installation.', 'error')
            return redirect(url_for('auth.setup'))

        # Upsert the repo designation
        db.execute(
            """
            INSERT INTO user_repos (user_id, repo_type, repo_full_name, installation_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, repo_type) DO UPDATE SET
                repo_full_name = excluded.repo_full_name,
                installation_id = excluded.installation_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, repo_type, repo_full_name, installation_id)
        )
        db.commit()

        _log_audit(user_id, 'configure', 'repo', repo_full_name, f'{{"type": "{repo_type}"}}')

        flash(f'Set {repo_full_name} as your {repo_type.title()} repository.', 'success')

    except Exception as e:
        logger.error(f"Failed to set repo: {e}")
        flash('Failed to configure repository.', 'error')

    return redirect(url_for('auth.setup'))


@auth_bp.route('/setup/apikey', methods=['POST'])
def setup_api_key():
    """Store an API key for BYK (Bring Your Key) tier users.

    POST params:
    - provider: 'anthropic' or 'openai'
    - api_key: The actual key (will be encrypted)
    """
    if 'user' not in session:
        return redirect(url_for('auth.login'))

    user = session['user']
    user_id = user.get('user_id')

    provider = request.form.get('provider')
    api_key = request.form.get('api_key')

    if provider not in ('anthropic', 'openai'):
        flash('Invalid API provider.', 'error')
        return redirect(url_for('auth.setup'))

    if not api_key:
        flash('API key is required.', 'error')
        return redirect(url_for('auth.setup'))

    try:
        from .crypto import encrypt_api_key

        db = _get_db()
        encrypted_key, key_hint = encrypt_api_key(user_id, api_key)

        # Upsert the API key
        db.execute(
            """
            INSERT INTO user_api_keys (user_id, provider, key_encrypted, key_hint, created_at, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, provider) DO UPDATE SET
                key_encrypted = excluded.key_encrypted,
                key_hint = excluded.key_hint,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, provider, encrypted_key, key_hint)
        )
        db.commit()

        _log_audit(user_id, 'configure', 'api_key', provider, f'{{"hint": "{key_hint}"}}')

        flash(f'Saved {provider.title()} API key (****{key_hint}).', 'success')

    except Exception as e:
        logger.error(f"Failed to store API key: {e}")
        flash('Failed to store API key.', 'error')

    return redirect(url_for('auth.setup'))


@auth_bp.route('/setup/create-library', methods=['POST'])
def setup_create_library():
    """Auto-create a Legato.Library repository for the user.

    Uses the user's first installation to create the Library repo.
    """
    if 'user' not in session:
        return redirect(url_for('auth.login'))

    user = session['user']
    user_id = user.get('user_id')
    github_login = user.get('username')

    try:
        from .chord_executor import ensure_library_exists

        db = _get_db()

        # Get user's first installation
        installation = db.execute(
            """
            SELECT installation_id, account_login
            FROM github_app_installations
            WHERE user_id = ?
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (user_id,)
        ).fetchone()

        if not installation:
            flash('Please install the GitHub App first.', 'error')
            return redirect(url_for('auth.setup'))

        # Get installation token
        token = get_user_installation_token(user_id, 'library')
        if not token:
            # Fall back to getting token directly
            from .github_app import get_installation_access_token
            token_data = get_installation_access_token(installation['installation_id'])
            token = token_data['token']

        # Use the installation's account (could be user or org)
        org = installation['account_login'] or github_login

        # Create Library repo
        result = ensure_library_exists(token, org)

        if result.get('success'):
            library_repo = f"{org}/Legato.Library"

            # Auto-configure as Library repo
            db.execute(
                """
                INSERT INTO user_repos (user_id, repo_type, repo_full_name, installation_id, created_at, updated_at)
                VALUES (?, 'library', ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, repo_type) DO UPDATE SET
                    repo_full_name = excluded.repo_full_name,
                    installation_id = excluded.installation_id,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, library_repo, installation['installation_id'])
            )
            db.commit()

            _log_audit(user_id, 'create', 'library', library_repo,
                       f'{{"created": {str(result.get("created", False)).lower()}}}')

            if result.get('created'):
                flash(f'Created {library_repo} as your Library.', 'success')
            else:
                flash(f'Configured existing {library_repo} as your Library.', 'success')
        else:
            flash('Failed to create Library repository.', 'error')

    except Exception as e:
        logger.error(f"Failed to create Library: {e}")
        flash(f'Failed to create Library: {str(e)}', 'error')

    return redirect(url_for('auth.setup'))


def get_current_user() -> Optional[dict]:
    """Get the current authenticated user.

    Returns:
        User dict from session, or None if not authenticated
    """
    return session.get('user')


def get_user_installation_token(user_id: str, repo_type: str = 'library') -> Optional[str]:
    """Get an installation access token for a user's designated repo.

    This is the key function for multi-tenant API access. It:
    1. Finds the user's designated repo of the given type
    2. Gets the installation that has access to it
    3. Returns a fresh access token (cached for performance)

    Args:
        user_id: The user's ID
        repo_type: 'library' or 'conduct'

    Returns:
        An access token string, or None if not available
    """
    from .github_app import get_token_manager

    db = _get_db()

    # Find the installation for this repo type
    row = db.execute(
        """
        SELECT ur.installation_id
        FROM user_repos ur
        WHERE ur.user_id = ? AND ur.repo_type = ?
        """,
        (user_id, repo_type)
    ).fetchone()

    if not row:
        logger.warning(f"No {repo_type} repo configured for user {user_id}")
        return None

    installation_id = row['installation_id']

    try:
        token_manager = get_token_manager(db)
        return token_manager.get_token(installation_id)
    except Exception as e:
        logger.error(f"Failed to get installation token: {e}")
        return None


def get_user_api_key(user_id: str, provider: str) -> Optional[str]:
    """Get a user's stored API key (decrypted).

    For BYK tier users who provide their own API keys.

    Args:
        user_id: The user's ID
        provider: 'anthropic' or 'openai'

    Returns:
        The decrypted API key, or None if not stored
    """
    from .crypto import decrypt_api_key

    db = _get_db()

    row = db.execute(
        "SELECT key_encrypted FROM user_api_keys WHERE user_id = ? AND provider = ?",
        (user_id, provider)
    ).fetchone()

    if not row:
        return None

    return decrypt_api_key(user_id, row['key_encrypted'])
