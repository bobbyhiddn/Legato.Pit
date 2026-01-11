"""
Transcript Dropbox for Legato.Pit

Secure transcript upload endpoint that triggers LEGATO processing.
Designed for mobile-first experience.

Security measures:
- Authentication required
- Rate limiting on uploads
- File type validation
- Size limits
- CSRF protection via session
"""
import os
import re
import logging
from datetime import datetime

import requests
from flask import (
    Blueprint, render_template, request, current_app,
    flash, redirect, url_for, jsonify
)

from .core import login_required

logger = logging.getLogger(__name__)

dropbox_bp = Blueprint('dropbox', __name__, url_prefix='/dropbox')

# Configuration
MAX_TRANSCRIPT_SIZE = 500 * 1024  # 500KB
ALLOWED_EXTENSIONS = {'txt', 'md', 'text'}


def allowed_file(filename):
    """Check if file extension is allowed."""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def sanitize_source_id(source_id):
    """Sanitize source identifier."""
    if not source_id:
        return f"dropbox-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
    # Remove any characters that aren't alphanumeric, dash, or underscore
    sanitized = re.sub(r'[^a-zA-Z0-9_-]', '-', source_id)
    return sanitized[:100]  # Limit length


def get_category_definitions():
    """Get user category definitions for classifier."""
    from flask import g
    from .rag.database import init_db, get_user_categories

    if 'legato_db_conn' not in g:
        g.legato_db_conn = init_db()

    categories = get_user_categories(g.legato_db_conn, 'default')

    # Format for classifier: list of {name, display_name, description, folder_name}
    return [
        {
            'name': cat['name'],
            'display_name': cat['display_name'],
            'description': cat.get('description', ''),
            'folder_name': cat['folder_name'],
        }
        for cat in categories
    ]


def dispatch_transcript(transcript_text, source_id):
    """
    Dispatch transcript to Legato.Conduct via repository_dispatch.

    Args:
        transcript_text: The transcript content
        source_id: Source identifier

    Returns:
        Tuple of (success: bool, message: str)
    """
    token = current_app.config.get('SYSTEM_PAT')
    if not token:
        logger.error("SYSTEM_PAT not configured")
        return False, "System not configured for transcript dispatch"

    org = current_app.config['LEGATO_ORG']
    repo = current_app.config['CONDUCT_REPO']

    # Get user-defined categories for dynamic classification
    category_definitions = get_category_definitions()
    logger.info(f"Sending {len(category_definitions)} category definitions to classifier")
    # Debug: Log category names being sent
    category_names = [c['name'] for c in category_definitions]
    logger.info(f"Category names being sent to Conduct: {category_names}")

    # Prepare dispatch payload
    # Include both 'transcript' and 'text' fields for compatibility with Conduct
    # Conduct's classifier may expect either field name depending on version
    payload = {
        'event_type': 'transcript-received',
        'client_payload': {
            'transcript': transcript_text,
            'text': transcript_text,  # Alias for compatibility
            'raw_text': transcript_text,  # For routing.json compatibility
            'source': source_id,
            'category_definitions': category_definitions,  # User-defined categories for classifier
        }
    }

    # Log dispatch details (truncate content for log readability)
    preview = transcript_text[:100] + '...' if len(transcript_text) > 100 else transcript_text
    logger.info(f"Dispatching transcript to Conduct: source={source_id}, length={len(transcript_text)} chars")
    logger.debug(f"Transcript preview: {preview!r}")

    try:
        response = requests.post(
            f'https://api.github.com/repos/{org}/{repo}/dispatches',
            json=payload,
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28'
            },
            timeout=15
        )

        if response.status_code == 204:
            logger.info(f"Transcript dispatched successfully: {source_id}")
            return True, "Transcript submitted for processing"
        else:
            logger.error(f"Dispatch failed: {response.status_code} - {response.text}")
            return False, f"Failed to dispatch transcript: {response.status_code}"

    except requests.RequestException as e:
        logger.error(f"Dispatch request failed: {e}")
        return False, "Network error while submitting transcript"


@dropbox_bp.route('/')
@login_required
def index():
    """Transcript upload form."""
    return render_template('dropbox.html', title='Transcript Dropbox')


@dropbox_bp.route('/upload', methods=['POST'])
@login_required
def upload():
    """Handle transcript upload."""
    # Get source identifier
    source_id = sanitize_source_id(request.form.get('source_id', ''))

    transcript_text = None

    # Check for text input first (more common use case)
    text_input = request.form.get('transcript', '').strip()
    if text_input:
        transcript_text = text_input

        if len(transcript_text.encode('utf-8')) > MAX_TRANSCRIPT_SIZE:
            flash(f'Transcript too long. Maximum size is {MAX_TRANSCRIPT_SIZE // 1024}KB.', 'error')
            return redirect(url_for('dropbox.index'))

    # Check for file upload if no text
    elif 'file' in request.files:
        file = request.files['file']

        # Only process if a file was actually selected
        if file.filename:
            if not allowed_file(file.filename):
                flash('Invalid file type. Please upload .txt or .md files.', 'error')
                return redirect(url_for('dropbox.index'))

            # Read file content
            content = file.read()

            if len(content) > MAX_TRANSCRIPT_SIZE:
                flash(f'File too large. Maximum size is {MAX_TRANSCRIPT_SIZE // 1024}KB.', 'error')
                return redirect(url_for('dropbox.index'))

            try:
                transcript_text = content.decode('utf-8')
            except UnicodeDecodeError:
                flash('Could not read file. Please ensure it is UTF-8 encoded text.', 'error')
                return redirect(url_for('dropbox.index'))

            # Use filename as source if not provided
            if not source_id or source_id.startswith('dropbox-'):
                source_id = sanitize_source_id(file.filename.rsplit('.', 1)[0])

    # No content provided
    if not transcript_text:
        flash('Please enter transcript text or upload a file.', 'error')
        return redirect(url_for('dropbox.index'))

    # Dispatch to LEGATO
    success, message = dispatch_transcript(transcript_text, source_id)

    if success:
        flash(f'{message} (Source: {source_id})', 'success')
    else:
        flash(message, 'error')

    return redirect(url_for('dropbox.index'))


@dropbox_bp.route('/api/debug-categories', methods=['GET'])
@login_required
def api_debug_categories():
    """
    Debug endpoint: Show exactly what category definitions would be sent to Conduct.

    This helps diagnose classification issues by showing:
    - All categories that will be passed to the classifier
    - Whether your new category is included
    - The description (which the classifier uses for context)
    """
    category_definitions = get_category_definitions()

    return jsonify({
        'count': len(category_definitions),
        'category_names': [c['name'] for c in category_definitions],
        'categories': category_definitions,
        'note': 'These are the exact category definitions that will be sent to Conduct for classification'
    })


@dropbox_bp.route('/api/upload', methods=['POST'])
@login_required
def api_upload():
    """
    API endpoint for transcript upload.
    Accepts JSON: {"transcript": "...", "source_id": "..."}
    """
    data = request.get_json()

    if not data or not data.get('transcript'):
        return jsonify({'error': 'Missing transcript field'}), 400

    transcript_text = data['transcript'].strip()
    source_id = sanitize_source_id(data.get('source_id', ''))

    if len(transcript_text.encode('utf-8')) > MAX_TRANSCRIPT_SIZE:
        return jsonify({'error': f'Transcript exceeds maximum size of {MAX_TRANSCRIPT_SIZE // 1024}KB'}), 400

    success, message = dispatch_transcript(transcript_text, source_id)

    if success:
        return jsonify({
            'success': True,
            'message': message,
            'source_id': source_id
        })
    else:
        return jsonify({'error': message}), 500
