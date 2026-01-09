"""
Agent Queue Blueprint

Handles queuing, approval, and spawning of Lab project agents.
Provides an approval gateway before Conduct spawns new repositories.
"""

import os
import json
import secrets
import logging
from datetime import datetime

import requests
from flask import Blueprint, request, jsonify, session, current_app, g, render_template

from .core import login_required

logger = logging.getLogger(__name__)

agents_bp = Blueprint('agents', __name__, url_prefix='/agents')


def get_db():
    """Get database connection."""
    if 'db_conn' not in g:
        from .rag.database import init_db
        g.db_conn = init_db()
    return g.db_conn


def generate_queue_id() -> str:
    """Generate a unique queue ID."""
    return f"aq-{secrets.token_hex(6)}"


def verify_system_token(req) -> bool:
    """Verify the request has a valid system token."""
    auth_header = req.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        token = auth_header[7:]
        system_pat = current_app.config.get('SYSTEM_PAT')
        return token == system_pat
    return False


# ============ Page Routes ============

@agents_bp.route('/')
@login_required
def index():
    """Agents queue management page."""
    db = get_db()

    # Get pending agents
    pending_rows = db.execute(
        """
        SELECT queue_id, project_name, project_type, title, description,
               source_transcript, created_at
        FROM agent_queue
        WHERE status = 'pending'
        ORDER BY created_at DESC
        """
    ).fetchall()
    pending_agents = [dict(row) for row in pending_rows]

    # Get recent processed agents (last 20)
    recent_rows = db.execute(
        """
        SELECT queue_id, project_name, project_type, title, status,
               approved_by, approved_at
        FROM agent_queue
        WHERE status != 'pending'
        ORDER BY updated_at DESC
        LIMIT 20
        """
    ).fetchall()
    recent_agents = [dict(row) for row in recent_rows]

    return render_template(
        'agents.html',
        pending_agents=pending_agents,
        recent_agents=recent_agents,
    )


# ============ API Endpoints (called by Conduct) ============

@agents_bp.route('/api/queue', methods=['POST'])
def api_queue_agent():
    """Queue a new agent for approval.

    Called by Conduct when a PROJECT thread is classified.
    Requires SYSTEM_PAT authentication.

    Request body:
    {
        "project_name": "MyProject",
        "project_type": "note" or "chord",
        "title": "Project Title",
        "description": "Project description",
        "signal_json": { ... },
        "tasker_body": "Issue body markdown",
        "source_transcript": "transcript-id"
    }

    Response:
    {
        "status": "queued",
        "queue_id": "aq-abc123def456"
    }
    """
    if not verify_system_token(request):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    required_fields = ['project_name', 'project_type', 'title', 'signal_json', 'tasker_body']
    for field in required_fields:
        if field not in data:
            return jsonify({'error': f'Missing required field: {field}'}), 400

    try:
        db = get_db()
        queue_id = generate_queue_id()

        # Serialize signal_json if it's a dict
        signal_json = data['signal_json']
        if isinstance(signal_json, dict):
            signal_json = json.dumps(signal_json)

        db.execute(
            """
            INSERT INTO agent_queue
            (queue_id, project_name, project_type, title, description,
             signal_json, tasker_body, source_transcript, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                queue_id,
                data['project_name'],
                data['project_type'],
                data['title'],
                data.get('description', ''),
                signal_json,
                data['tasker_body'],
                data.get('source_transcript'),
            )
        )
        db.commit()

        logger.info(f"Queued agent: {queue_id} - {data['project_name']}")

        return jsonify({
            'status': 'queued',
            'queue_id': queue_id,
        })

    except Exception as e:
        logger.error(f"Failed to queue agent: {e}")
        return jsonify({'error': str(e)}), 500


@agents_bp.route('/api/pending', methods=['GET'])
@login_required
def api_list_pending():
    """List all pending agents.

    Response:
    {
        "agents": [
            {
                "queue_id": "aq-abc123",
                "project_name": "MyProject",
                "project_type": "note",
                "title": "Project Title",
                "description": "...",
                "source_transcript": "...",
                "created_at": "2026-01-09T..."
            }
        ],
        "count": 1
    }
    """
    try:
        db = get_db()
        rows = db.execute(
            """
            SELECT queue_id, project_name, project_type, title, description,
                   source_transcript, created_at
            FROM agent_queue
            WHERE status = 'pending'
            ORDER BY created_at DESC
            """
        ).fetchall()

        agents = [dict(row) for row in rows]

        return jsonify({
            'agents': agents,
            'count': len(agents),
        })

    except Exception as e:
        logger.error(f"Failed to list pending agents: {e}")
        return jsonify({'error': str(e)}), 500


@agents_bp.route('/api/<queue_id>/approve', methods=['POST'])
@login_required
def api_approve_agent(queue_id: str):
    """Approve an agent and trigger spawn.

    This triggers the Conduct spawn-project workflow via repository_dispatch.

    Response:
    {
        "status": "approved",
        "queue_id": "aq-abc123",
        "dispatch_sent": true
    }
    """
    try:
        db = get_db()

        # Get the queued agent
        row = db.execute(
            "SELECT * FROM agent_queue WHERE queue_id = ? AND status = 'pending'",
            (queue_id,)
        ).fetchone()

        if not row:
            return jsonify({'error': 'Agent not found or already processed'}), 404

        agent = dict(row)
        user = session.get('user', {})
        username = user.get('login', 'unknown')

        # Trigger Conduct spawn workflow
        dispatch_result = trigger_spawn_workflow(agent)

        # Update status
        db.execute(
            """
            UPDATE agent_queue
            SET status = 'approved',
                approved_by = ?,
                approved_at = CURRENT_TIMESTAMP,
                spawn_result = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE queue_id = ?
            """,
            (username, json.dumps(dispatch_result), queue_id)
        )
        db.commit()

        logger.info(f"Approved agent: {queue_id} by {username}")

        return jsonify({
            'status': 'approved',
            'queue_id': queue_id,
            'dispatch_sent': dispatch_result.get('success', False),
        })

    except Exception as e:
        logger.error(f"Failed to approve agent: {e}")
        return jsonify({'error': str(e)}), 500


@agents_bp.route('/api/<queue_id>/reject', methods=['POST'])
@login_required
def api_reject_agent(queue_id: str):
    """Reject an agent (won't spawn).

    Request body (optional):
    {
        "reason": "Not needed"
    }

    Response:
    {
        "status": "rejected",
        "queue_id": "aq-abc123"
    }
    """
    try:
        db = get_db()
        data = request.get_json() or {}
        reason = data.get('reason', '')

        user = session.get('user', {})
        username = user.get('login', 'unknown')

        result = db.execute(
            """
            UPDATE agent_queue
            SET status = 'rejected',
                approved_by = ?,
                approved_at = CURRENT_TIMESTAMP,
                spawn_result = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE queue_id = ? AND status = 'pending'
            """,
            (username, json.dumps({'rejected': True, 'reason': reason}), queue_id)
        )
        db.commit()

        if result.rowcount == 0:
            return jsonify({'error': 'Agent not found or already processed'}), 404

        logger.info(f"Rejected agent: {queue_id} by {username}")

        return jsonify({
            'status': 'rejected',
            'queue_id': queue_id,
        })

    except Exception as e:
        logger.error(f"Failed to reject agent: {e}")
        return jsonify({'error': str(e)}), 500


@agents_bp.route('/api/<queue_id>', methods=['GET'])
@login_required
def api_get_agent(queue_id: str):
    """Get details of a specific queued agent."""
    try:
        db = get_db()
        row = db.execute(
            "SELECT * FROM agent_queue WHERE queue_id = ?",
            (queue_id,)
        ).fetchone()

        if not row:
            return jsonify({'error': 'Agent not found'}), 404

        agent = dict(row)
        # Parse JSON fields
        if agent.get('signal_json'):
            try:
                agent['signal_json'] = json.loads(agent['signal_json'])
            except json.JSONDecodeError:
                pass
        if agent.get('spawn_result'):
            try:
                agent['spawn_result'] = json.loads(agent['spawn_result'])
            except json.JSONDecodeError:
                pass

        return jsonify(agent)

    except Exception as e:
        logger.error(f"Failed to get agent: {e}")
        return jsonify({'error': str(e)}), 500


def trigger_spawn_workflow(agent: dict) -> dict:
    """Trigger the Conduct spawn-project workflow via repository_dispatch.

    Args:
        agent: Agent dict from database

    Returns:
        Dict with success status and details
    """
    token = current_app.config.get('SYSTEM_PAT')
    if not token:
        return {'success': False, 'error': 'SYSTEM_PAT not configured'}

    org = current_app.config.get('LEGATO_ORG', 'bobbyhiddn')
    conduct_repo = current_app.config.get('CONDUCT_REPO', 'Legato.Conduct')

    # Parse signal_json if it's a string
    signal_json = agent.get('signal_json', '{}')
    if isinstance(signal_json, str):
        try:
            signal_json = json.loads(signal_json)
        except json.JSONDecodeError:
            signal_json = {}

    payload = {
        'event_type': 'spawn-agent',
        'client_payload': {
            'queue_id': agent['queue_id'],
            'project_name': agent['project_name'],
            'project_type': agent['project_type'],
            'signal_json': signal_json,
            'tasker_body': agent['tasker_body'],
        }
    }

    try:
        response = requests.post(
            f'https://api.github.com/repos/{org}/{conduct_repo}/dispatches',
            json=payload,
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2022-11-28',
            },
            timeout=15,
        )

        # 204 No Content = success
        if response.status_code == 204:
            logger.info(f"Triggered spawn workflow for {agent['queue_id']}")
            return {'success': True}
        else:
            logger.error(f"Dispatch failed: {response.status_code} - {response.text}")
            return {'success': False, 'error': f"HTTP {response.status_code}"}

    except requests.RequestException as e:
        logger.error(f"Dispatch request failed: {e}")
        return {'success': False, 'error': str(e)}
