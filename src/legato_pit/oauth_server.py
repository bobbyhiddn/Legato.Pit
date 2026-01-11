"""
OAuth 2.1 Authorization Server with Dynamic Client Registration

Implements RFC 7591 (DCR) and RFC 8414 (OAuth Server Metadata)
to enable Claude.ai MCP connector integration.

Authentication flow:
1. Claude discovers OAuth metadata via /.well-known/oauth-authorization-server
2. Claude registers itself via /oauth/register (DCR)
3. User is redirected to GitHub for authentication
4. Pit issues its own JWT tokens to Claude (not GitHub tokens)
5. Claude uses these tokens to access MCP endpoints
"""

import os
import secrets
import hashlib
import base64
import logging
import json
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlencode

import requests
import jwt
from flask import Blueprint, request, jsonify, redirect, session, current_app, g, url_for

logger = logging.getLogger(__name__)

oauth_bp = Blueprint('oauth', __name__)

# GitHub OAuth endpoints (reused from auth.py)
GITHUB_AUTHORIZE_URL = 'https://github.com/login/oauth/authorize'
GITHUB_TOKEN_URL = 'https://github.com/login/oauth/access_token'
GITHUB_USER_URL = 'https://api.github.com/user'


def get_db():
    """Get legato database connection for OAuth tables."""
    if 'oauth_db_conn' not in g:
        from .rag.database import init_db
        g.oauth_db_conn = init_db()
    return g.oauth_db_conn


def get_jwt_secret() -> str:
    """Get JWT secret key, falling back to Flask secret if not set."""
    return os.environ.get('JWT_SECRET_KEY') or current_app.config.get('SECRET_KEY')


def get_base_url() -> str:
    """Get the base URL respecting proxy headers and PREFERRED_URL_SCHEME.

    Uses url_for with _external=True to properly handle Fly.io proxy.
    """
    # url_for with _external=True respects PREFERRED_URL_SCHEME and ProxyFix
    root = url_for('oauth.oauth_discovery', _external=True)
    # Strip the endpoint path to get base URL
    return root.rsplit('/.well-known/', 1)[0]


# ============ OAuth Discovery ============

@oauth_bp.route('/oauth/debug')
def oauth_debug():
    """Debug endpoint to verify URL generation (remove in production)."""
    return jsonify({
        "base_url": get_base_url(),
        "github_callback_url": url_for('auth.github_callback', _external=True),
        "note": "MCP OAuth uses the same callback as web login",
        "request_url": request.url,
        "request_host": request.host,
        "x_forwarded_proto": request.headers.get('X-Forwarded-Proto'),
        "x_forwarded_host": request.headers.get('X-Forwarded-Host'),
    })


@oauth_bp.route('/.well-known/oauth-authorization-server')
def oauth_discovery():
    """OAuth 2.1 Authorization Server Metadata (RFC 8414).

    Claude.ai uses this to discover OAuth endpoints for DCR and authorization.
    """
    base = get_base_url()

    return jsonify({
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp:read", "mcp:write"],
        "service_documentation": f"{base}/docs/mcp"
    })


@oauth_bp.route('/.well-known/oauth-protected-resource')
def oauth_protected_resource():
    """OAuth 2.1 Protected Resource Metadata (RFC 9728)."""
    base = get_base_url()

    return jsonify({
        "resource": base,
        "authorization_servers": [base]
    })


# ============ Dynamic Client Registration (RFC 7591) ============

@oauth_bp.route('/oauth/register', methods=['POST'])
def register_client():
    """Dynamic Client Registration endpoint.

    Claude.ai registers itself as an OAuth client before initiating auth flow.
    We store the client in the database for persistence across restarts.

    Request:
    {
        "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
        "client_name": "Claude",
        "token_endpoint_auth_method": "none"
    }

    Response:
    {
        "client_id": "mcp-xxxxxxxx",
        "client_secret": null,
        "redirect_uris": [...],
        "client_name": "Claude"
    }
    """
    data = request.get_json() or {}

    redirect_uris = data.get('redirect_uris', [])
    client_name = data.get('client_name', 'Unknown MCP Client')

    # Validate redirect URIs
    if not redirect_uris:
        return jsonify({
            "error": "invalid_redirect_uri",
            "error_description": "At least one redirect_uri is required"
        }), 400

    # Generate client ID
    client_id = f"mcp-{secrets.token_hex(8)}"

    # Store in database
    db = get_db()
    try:
        db.execute("""
            INSERT INTO oauth_clients (client_id, client_name, redirect_uris)
            VALUES (?, ?, ?)
        """, (client_id, client_name, json.dumps(redirect_uris)))
        db.commit()
    except Exception as e:
        logger.error(f"Failed to register OAuth client: {e}")
        return jsonify({
            "error": "server_error",
            "error_description": "Failed to register client"
        }), 500

    logger.info(f"Registered OAuth client: {client_id} ({client_name})")

    return jsonify({
        "client_id": client_id,
        "client_secret": None,  # Public client (no secret)
        "redirect_uris": redirect_uris,
        "client_name": client_name,
        "token_endpoint_auth_method": "none"
    }), 201


# ============ Authorization Flow ============

@oauth_bp.route('/oauth/authorize')
def authorize():
    """OAuth authorization endpoint.

    Validates the OAuth request, then redirects to GitHub for authentication.
    After GitHub auth, we redirect back to Claude with our own auth code.

    Query params:
    - client_id: From DCR registration
    - redirect_uri: Where to send the auth code
    - state: CSRF protection token
    - code_challenge: PKCE challenge (S256)
    - code_challenge_method: Must be "S256"
    - response_type: Must be "code"
    - scope: Optional scopes
    """
    client_id = request.args.get('client_id')
    redirect_uri = request.args.get('redirect_uri')
    state = request.args.get('state')
    code_challenge = request.args.get('code_challenge')
    code_challenge_method = request.args.get('code_challenge_method', 'S256')
    response_type = request.args.get('response_type', 'code')
    scope = request.args.get('scope', 'mcp:read mcp:write')

    # Validate required parameters
    if not client_id:
        return jsonify({"error": "invalid_request", "error_description": "client_id required"}), 400
    if not redirect_uri:
        return jsonify({"error": "invalid_request", "error_description": "redirect_uri required"}), 400
    if response_type != 'code':
        return jsonify({"error": "unsupported_response_type"}), 400
    if code_challenge_method != 'S256':
        return jsonify({"error": "invalid_request", "error_description": "Only S256 PKCE supported"}), 400

    # Verify client is registered
    db = get_db()
    client = db.execute(
        "SELECT * FROM oauth_clients WHERE client_id = ?",
        (client_id,)
    ).fetchone()

    if not client:
        return jsonify({"error": "invalid_client", "error_description": "Client not registered"}), 400

    # Verify redirect_uri matches registration
    registered_uris = json.loads(client['redirect_uris'])
    if redirect_uri not in registered_uris:
        return jsonify({
            "error": "invalid_redirect_uri",
            "error_description": "redirect_uri not registered for this client"
        }), 400

    # Store OAuth request in session for after GitHub callback
    session['mcp_oauth_request'] = {
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'state': state,
        'code_challenge': code_challenge,
        'scope': scope
    }

    # Generate state for GitHub OAuth (separate from Claude's state)
    github_state = secrets.token_urlsafe(32)
    session['mcp_github_state'] = github_state

    # Redirect to GitHub OAuth
    github_client_id = current_app.config.get('GITHUB_CLIENT_ID')
    if not github_client_id:
        return jsonify({
            "error": "server_error",
            "error_description": "GitHub OAuth not configured"
        }), 500

    # Use the existing auth callback URL (GitHub only allows one)
    # auth.py will detect MCP flow via session and redirect to us
    params = {
        'client_id': github_client_id,
        'redirect_uri': url_for('auth.github_callback', _external=True),
        'scope': 'read:user',
        'state': github_state
    }

    github_auth_url = f"{GITHUB_AUTHORIZE_URL}?{urlencode(params)}"
    logger.info(f"MCP OAuth: redirecting to GitHub for client {client_id}")

    return redirect(github_auth_url)


def handle_mcp_github_callback():
    """Handle GitHub OAuth callback for MCP authorization.

    Called from auth.py when it detects an MCP OAuth flow.
    Exchanges GitHub code for user info, then generates our own auth code
    and redirects back to Claude.
    """
    # Verify GitHub state
    state = request.args.get('state')
    stored_state = session.pop('mcp_github_state', None)

    if not state or state != stored_state:
        logger.warning("MCP OAuth: GitHub state mismatch")
        return jsonify({"error": "invalid_state"}), 400

    # Get stored OAuth request
    oauth_request = session.pop('mcp_oauth_request', None)
    if not oauth_request:
        logger.warning("MCP OAuth: No stored OAuth request")
        return jsonify({"error": "invalid_request", "error_description": "Session expired"}), 400

    # Check for GitHub errors
    error = request.args.get('error')
    if error:
        error_desc = request.args.get('error_description', 'Authentication failed')
        callback = f"{oauth_request['redirect_uri']}?error={error}&error_description={error_desc}"
        if oauth_request.get('state'):
            callback += f"&state={oauth_request['state']}"
        return redirect(callback)

    # Get GitHub authorization code
    code = request.args.get('code')
    if not code:
        return jsonify({"error": "invalid_request", "error_description": "No code from GitHub"}), 400

    # Exchange code for GitHub access token (use auth callback URL since that's what GitHub expects)
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
        logger.error(f"MCP OAuth: Failed to exchange GitHub code: {e}")
        callback = f"{oauth_request['redirect_uri']}?error=server_error&error_description=GitHub+token+exchange+failed"
        if oauth_request.get('state'):
            callback += f"&state={oauth_request['state']}"
        return redirect(callback)

    github_token = token_json.get('access_token')
    if not github_token:
        logger.warning(f"MCP OAuth: No access token from GitHub: {token_json}")
        callback = f"{oauth_request['redirect_uri']}?error=server_error&error_description=No+access+token"
        if oauth_request.get('state'):
            callback += f"&state={oauth_request['state']}"
        return redirect(callback)

    # Fetch GitHub user info
    try:
        user_response = requests.get(
            GITHUB_USER_URL,
            headers={
                'Authorization': f'Bearer {github_token}',
                'Accept': 'application/vnd.github+json'
            },
            timeout=10
        )
        user_response.raise_for_status()
        github_user = user_response.json()
    except requests.RequestException as e:
        logger.error(f"MCP OAuth: Failed to fetch GitHub user: {e}")
        callback = f"{oauth_request['redirect_uri']}?error=server_error&error_description=Failed+to+fetch+user"
        if oauth_request.get('state'):
            callback += f"&state={oauth_request['state']}"
        return redirect(callback)

    # Verify user is in allowlist
    allowed_users = current_app.config.get('GITHUB_ALLOWED_USERS', [])
    allowed_users = [u.strip() for u in allowed_users if u.strip()]

    if allowed_users and github_user.get('login') not in allowed_users:
        logger.warning(f"MCP OAuth: Unauthorized user: {github_user.get('login')}")
        callback = f"{oauth_request['redirect_uri']}?error=access_denied&error_description=User+not+authorized"
        if oauth_request.get('state'):
            callback += f"&state={oauth_request['state']}"
        return redirect(callback)

    # Generate our own authorization code
    auth_code = secrets.token_urlsafe(32)

    # Store auth code with user info and PKCE challenge
    db = get_db()
    expires_at = datetime.utcnow() + timedelta(minutes=5)

    db.execute("""
        INSERT INTO oauth_auth_codes (code, client_id, github_user_id, github_login,
                                      code_challenge, scope, redirect_uri, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        auth_code,
        oauth_request['client_id'],
        github_user.get('id'),
        github_user.get('login'),
        oauth_request.get('code_challenge'),
        oauth_request.get('scope', 'mcp:read mcp:write'),
        oauth_request['redirect_uri'],
        expires_at.isoformat()
    ))
    db.commit()

    logger.info(f"MCP OAuth: Issued auth code for {github_user.get('login')} to client {oauth_request['client_id']}")

    # Redirect back to Claude with our auth code
    callback = f"{oauth_request['redirect_uri']}?code={auth_code}"
    if oauth_request.get('state'):
        callback += f"&state={oauth_request['state']}"

    return redirect(callback)


# ============ Token Endpoint ============

@oauth_bp.route('/oauth/token', methods=['POST'])
def token():
    """Token endpoint - exchange auth code for access token.

    Implements PKCE verification and issues JWT access tokens.

    Request (application/x-www-form-urlencoded):
    - grant_type: "authorization_code" or "refresh_token"
    - code: Authorization code (for authorization_code grant)
    - code_verifier: PKCE verifier (for authorization_code grant)
    - redirect_uri: Must match original
    - refresh_token: For refresh_token grant

    Response:
    {
        "access_token": "eyJ...",
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": "..."
    }
    """
    grant_type = request.form.get('grant_type')

    if grant_type == 'authorization_code':
        return _handle_authorization_code_grant()
    elif grant_type == 'refresh_token':
        return _handle_refresh_token_grant()
    else:
        return jsonify({
            "error": "unsupported_grant_type",
            "error_description": f"Grant type '{grant_type}' not supported"
        }), 400


def _handle_authorization_code_grant():
    """Handle authorization_code grant type."""
    code = request.form.get('code')
    code_verifier = request.form.get('code_verifier')
    redirect_uri = request.form.get('redirect_uri')
    client_id = request.form.get('client_id')

    if not code:
        return jsonify({"error": "invalid_request", "error_description": "code required"}), 400

    # Look up auth code
    db = get_db()
    auth_code = db.execute(
        "SELECT * FROM oauth_auth_codes WHERE code = ?",
        (code,)
    ).fetchone()

    if not auth_code:
        return jsonify({"error": "invalid_grant", "error_description": "Invalid or expired code"}), 400

    # Delete the code (one-time use)
    db.execute("DELETE FROM oauth_auth_codes WHERE code = ?", (code,))
    db.commit()

    # Check expiration
    expires_at = datetime.fromisoformat(auth_code['expires_at'])
    if datetime.utcnow() > expires_at:
        return jsonify({"error": "invalid_grant", "error_description": "Code expired"}), 400

    # Verify redirect_uri matches
    if redirect_uri and redirect_uri != auth_code['redirect_uri']:
        return jsonify({"error": "invalid_grant", "error_description": "redirect_uri mismatch"}), 400

    # Verify PKCE code_verifier
    if auth_code['code_challenge']:
        if not code_verifier:
            return jsonify({"error": "invalid_grant", "error_description": "code_verifier required"}), 400

        # Compute S256 hash of verifier
        verifier_hash = hashlib.sha256(code_verifier.encode('ascii')).digest()
        computed_challenge = base64.urlsafe_b64encode(verifier_hash).rstrip(b'=').decode('ascii')

        if computed_challenge != auth_code['code_challenge']:
            logger.warning(f"MCP OAuth: PKCE verification failed for client {auth_code['client_id']}")
            return jsonify({"error": "invalid_grant", "error_description": "PKCE verification failed"}), 400

    # Generate access token (JWT)
    access_token = _create_access_token(
        github_login=auth_code['github_login'],
        github_user_id=auth_code['github_user_id'],
        client_id=auth_code['client_id'],
        scope=auth_code['scope']
    )

    # Generate refresh token
    refresh_token = secrets.token_urlsafe(32)
    refresh_expires = datetime.utcnow() + timedelta(days=30)

    db.execute("""
        INSERT INTO oauth_sessions (client_id, github_user_id, github_login, refresh_token, expires_at)
        VALUES (?, ?, ?, ?, ?)
    """, (
        auth_code['client_id'],
        auth_code['github_user_id'],
        auth_code['github_login'],
        refresh_token,
        refresh_expires.isoformat()
    ))
    db.commit()

    logger.info(f"MCP OAuth: Issued tokens for {auth_code['github_login']}")

    return jsonify({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": refresh_token,
        "scope": auth_code['scope']
    })


def _handle_refresh_token_grant():
    """Handle refresh_token grant type."""
    refresh_token = request.form.get('refresh_token')

    if not refresh_token:
        return jsonify({"error": "invalid_request", "error_description": "refresh_token required"}), 400

    # Look up refresh token
    db = get_db()
    session_row = db.execute(
        "SELECT * FROM oauth_sessions WHERE refresh_token = ?",
        (refresh_token,)
    ).fetchone()

    if not session_row:
        return jsonify({"error": "invalid_grant", "error_description": "Invalid refresh token"}), 400

    # Check expiration
    expires_at = datetime.fromisoformat(session_row['expires_at'])
    if datetime.utcnow() > expires_at:
        db.execute("DELETE FROM oauth_sessions WHERE refresh_token = ?", (refresh_token,))
        db.commit()
        return jsonify({"error": "invalid_grant", "error_description": "Refresh token expired"}), 400

    # Generate new access token
    access_token = _create_access_token(
        github_login=session_row['github_login'],
        github_user_id=session_row['github_user_id'],
        client_id=session_row['client_id'],
        scope='mcp:read mcp:write'
    )

    # Rotate refresh token
    new_refresh_token = secrets.token_urlsafe(32)
    new_expires = datetime.utcnow() + timedelta(days=30)

    db.execute("""
        UPDATE oauth_sessions
        SET refresh_token = ?, expires_at = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (new_refresh_token, new_expires.isoformat(), session_row['id']))
    db.commit()

    logger.info(f"MCP OAuth: Refreshed tokens for {session_row['github_login']}")

    return jsonify({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": new_refresh_token
    })


# ============ Token Utilities ============

def _create_access_token(github_login: str, github_user_id: int, client_id: str, scope: str) -> str:
    """Create a signed JWT access token."""
    now = datetime.utcnow()
    payload = {
        "sub": github_login,
        "github_id": github_user_id,
        "client_id": client_id,
        "scope": scope,
        "iat": now,
        "exp": now + timedelta(hours=1),
        "iss": "legato-pit"
    }

    return jwt.encode(payload, get_jwt_secret(), algorithm="HS256")


def verify_access_token(token: str) -> dict | None:
    """Verify a JWT access token and return claims if valid."""
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        logger.debug("MCP OAuth: Token expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.debug(f"MCP OAuth: Invalid token: {e}")
        return None


def require_mcp_auth(f):
    """Decorator to require valid MCP access token."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')

        if not auth_header.startswith('Bearer '):
            return jsonify({"error": "unauthorized"}), 401, {
                'WWW-Authenticate': 'Bearer realm="legato-pit"'
            }

        token = auth_header[7:]
        claims = verify_access_token(token)

        if not claims:
            return jsonify({"error": "invalid_token"}), 401, {
                'WWW-Authenticate': 'Bearer realm="legato-pit", error="invalid_token"'
            }

        # Store claims in g for use in handler
        g.mcp_user = claims

        return f(*args, **kwargs)
    return decorated
