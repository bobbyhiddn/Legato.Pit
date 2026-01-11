"""
GitHub OAuth Authentication for Legato.Pit

Security measures:
- State parameter to prevent CSRF attacks
- User allowlist enforcement
- Secure token handling
- Session fixation protection
"""
import os
import secrets
import logging
from urllib.parse import urlencode

import requests
from flask import (
    Blueprint, redirect, url_for, session, request,
    current_app, flash, render_template
)

logger = logging.getLogger(__name__)

auth_bp = Blueprint('auth', __name__, url_prefix='/auth')

# GitHub OAuth endpoints
GITHUB_AUTHORIZE_URL = 'https://github.com/login/oauth/authorize'
GITHUB_TOKEN_URL = 'https://github.com/login/oauth/access_token'
GITHUB_USER_URL = 'https://api.github.com/user'


@auth_bp.route('/login')
def login():
    """Display login page or initiate OAuth flow."""
    if 'user' in session:
        return redirect(url_for('dashboard.index'))

    # Check if OAuth is configured
    if not current_app.config.get('GITHUB_CLIENT_ID'):
        flash('GitHub OAuth not configured. Contact administrator.', 'error')
        return render_template('login.html', oauth_configured=False)

    return render_template('login.html', oauth_configured=True)


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
