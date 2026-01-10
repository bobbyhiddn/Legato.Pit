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
    """Get agents database connection."""
    if 'agents_db_conn' not in g:
        from .rag.database import init_agents_db
        g.agents_db_conn = init_agents_db()
    return g.agents_db_conn


def get_legato_db():
    """Get legato database connection (for knowledge entries)."""
    if 'legato_db_conn' not in g:
        from .rag.database import init_db
        g.legato_db_conn = init_db()
    return g.legato_db_conn


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


# ============ API Endpoints (called by Pit UI) ============

@agents_bp.route('/api/from-entry', methods=['POST'])
@login_required
def api_queue_from_entry():
    """Queue an agent to create a Chord (Lab repo) from a Note (library entry).

    Request body:
    {
        "entry_id": "kb-abc123"
    }

    Response:
    {
        "status": "queued",
        "queue_id": "aq-abc123def456"
    }
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    entry_id = data.get('entry_id')
    project_name = data.get('project_name')

    if not entry_id:
        return jsonify({'error': 'Missing entry_id'}), 400

    if not project_name:
        return jsonify({'error': 'Missing project_name'}), 400

    # Validate project name
    import re
    project_name = re.sub(r'[^a-z0-9-]', '', project_name.lower())[:30]
    if len(project_name) < 2:
        return jsonify({'error': 'Project name must be at least 2 characters'}), 400

    try:
        legato_db = get_legato_db()
        agents_db = get_db()

        # Get the library entry from legato.db
        entry = legato_db.execute(
            "SELECT * FROM knowledge_entries WHERE entry_id = ?",
            (entry_id,)
        ).fetchone()

        if not entry:
            return jsonify({'error': 'Entry not found'}), 404

        entry = dict(entry)

        # Build tasker body from entry content
        content_preview = entry['content'][:500] if entry['content'] else ''
        tasker_body = f"""## Tasker: {entry['title']}

### Context
From knowledge entry `{entry_id}`:
"{content_preview}"

### Objective
Implement the project as described in the knowledge entry.

### Acceptance Criteria
- [ ] Core functionality implemented
- [ ] Documentation updated
- [ ] Tests written

### Constraints
- Follow patterns in `copilot-instructions.md`
- Reference `SIGNAL.md` for project intent
- Keep PRs focused and reviewable

### References
- Source entry: `{entry_id}`
- Category: {entry.get('category', 'general')}

---
*Generated from Pit library entry | Source: {entry_id}*
"""

        # Build signal JSON - always creates a Chord (repo) from a Note (entry)
        signal_json = {
            "id": f"lab.chord.{project_name}",
            "type": "project",
            "source": "pit-library",
            "category": "chord",
            "title": entry['title'],
            "domain_tags": [],
            "intent": entry.get('content', '')[:200],
            "key_phrases": [],
            "path": f"Lab.{project_name}.Chord",
        }

        queue_id = generate_queue_id()

        agents_db.execute(
            """
            INSERT INTO agent_queue
            (queue_id, project_name, project_type, title, description,
             signal_json, tasker_body, source_transcript, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                queue_id,
                project_name,
                'chord',  # Always chord - we're creating a repo from a note
                entry['title'],
                entry.get('content', '')[:500],
                json.dumps(signal_json),
                tasker_body,
                f"library:{entry_id}",
            )
        )
        agents_db.commit()

        logger.info(f"Queued agent from entry: {queue_id} - {project_name}")

        return jsonify({
            'status': 'queued',
            'queue_id': queue_id,
            'project_name': project_name,
        })

    except Exception as e:
        logger.error(f"Failed to queue from entry: {e}")
        return jsonify({'error': str(e)}), 500


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


# ============ GitHub Artifact Sync ============

def fetch_conduct_workflow_runs(token: str, org: str, repo: str, limit: int = 10) -> list:
    """Fetch recent process-transcript workflow runs from Conduct.

    Args:
        token: GitHub PAT
        org: GitHub org
        repo: Conduct repo name
        limit: Max runs to fetch

    Returns:
        List of workflow run dicts
    """
    try:
        response = requests.get(
            f'https://api.github.com/repos/{org}/{repo}/actions/workflows/process-transcript.yml/runs',
            params={'per_page': limit, 'status': 'completed'},
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': 'application/vnd.github+json',
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        return data.get('workflow_runs', [])
    except requests.RequestException as e:
        logger.error(f"Failed to fetch workflow runs: {e}")
        return []


def fetch_routing_artifact(token: str, org: str, repo: str, run_id: int) -> dict | None:
    """Download and parse routing-decisions artifact from a workflow run.

    Args:
        token: GitHub PAT
        org: GitHub org
        repo: Conduct repo name
        run_id: Workflow run ID

    Returns:
        Parsed routing.json dict, or None if not found
    """
    import zipfile
    import io

    try:
        # List artifacts for the run
        response = requests.get(
            f'https://api.github.com/repos/{org}/{repo}/actions/runs/{run_id}/artifacts',
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': 'application/vnd.github+json',
            },
            timeout=15,
        )
        response.raise_for_status()
        artifacts = response.json().get('artifacts', [])

        # Find routing-decisions artifact
        routing_artifact = None
        for artifact in artifacts:
            if artifact['name'] == 'routing-decisions':
                routing_artifact = artifact
                break

        if not routing_artifact:
            return None

        # Download the artifact (it's a zip file)
        download_url = routing_artifact['archive_download_url']
        response = requests.get(
            download_url,
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': 'application/vnd.github+json',
            },
            timeout=30,
        )
        response.raise_for_status()

        # Extract routing.json from zip
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            with zf.open('routing.json') as f:
                return json.load(f)

    except Exception as e:
        logger.error(f"Failed to fetch routing artifact for run {run_id}: {e}")
        return None


def import_projects_from_routing(routing: list, run_id: int, db) -> dict:
    """Import PROJECT items from routing data into agent_queue.

    Args:
        routing: Parsed routing.json list
        run_id: Workflow run ID (for source tracking)
        db: Database connection

    Returns:
        Dict with import stats
    """
    stats = {'found': 0, 'imported': 0, 'skipped': 0, 'errors': 0}

    for item in routing:
        if item.get('type') != 'PROJECT':
            continue

        stats['found'] += 1

        project_name = item.get('project_name', 'unnamed')
        source_id = f"conduct-run:{run_id}:{item.get('id', 'unknown')}"

        # Check if already imported
        existing = db.execute(
            "SELECT queue_id FROM agent_queue WHERE source_transcript = ?",
            (source_id,)
        ).fetchone()

        if existing:
            stats['skipped'] += 1
            continue

        try:
            # Build tasker body
            description = item.get('project_description') or item.get('description') or ''
            raw_text = item.get('raw_text', '')[:500]

            tasker_body = f"""## Tasker: {item.get('knowledge_title') or item.get('title', 'Untitled')}

### Context
From voice transcript:
"{raw_text}"

### Objective
{description or 'Implement the project as described.'}

### Acceptance Criteria
{chr(10).join(f"- [ ] {kp}" for kp in item.get('key_phrases', [])[:5]) or '- [ ] Core functionality implemented'}

### Constraints
- Follow patterns in `copilot-instructions.md`
- Reference `SIGNAL.md` for project intent
- Keep PRs focused and reviewable

### References
- Source: Conduct workflow run {run_id}
- Thread: {item.get('id', 'unknown')}

---
*Generated from Conduct pipeline*
"""

            signal_json = {
                "id": f"lab.{item.get('project_scope', 'chord')}.{project_name}",
                "type": "project",
                "source": "conduct",
                "category": item.get('project_scope', 'chord'),
                "title": item.get('knowledge_title') or item.get('title', 'Untitled'),
                "domain_tags": item.get('domain_tags', []),
                "key_phrases": item.get('key_phrases', []),
            }

            queue_id = generate_queue_id()

            db.execute(
                """
                INSERT INTO agent_queue
                (queue_id, project_name, project_type, title, description,
                 signal_json, tasker_body, source_transcript, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                (
                    queue_id,
                    project_name,
                    item.get('project_scope', 'chord'),
                    item.get('knowledge_title') or item.get('title', 'Untitled'),
                    description[:500],
                    json.dumps(signal_json),
                    tasker_body,
                    source_id,
                )
            )
            stats['imported'] += 1
            logger.info(f"Imported project from Conduct: {queue_id} - {project_name}")

        except Exception as e:
            logger.error(f"Failed to import project {project_name}: {e}")
            stats['errors'] += 1

    db.commit()
    return stats


@agents_bp.route('/api/sync', methods=['POST'])
@login_required
def api_sync_from_conduct():
    """Sync pending projects from Conduct workflow artifacts.

    Fetches recent process-transcript workflow runs, downloads routing-decisions
    artifacts, and imports PROJECT items into the agent queue.

    Response:
    {
        "status": "synced",
        "runs_checked": 5,
        "projects_found": 2,
        "projects_imported": 1,
        "projects_skipped": 1
    }
    """
    token = current_app.config.get('SYSTEM_PAT')
    if not token:
        return jsonify({'error': 'SYSTEM_PAT not configured'}), 500

    org = current_app.config.get('LEGATO_ORG', 'bobbyhiddn')
    conduct_repo = current_app.config.get('CONDUCT_REPO', 'Legato.Conduct')

    try:
        db = get_db()

        # Fetch recent workflow runs
        runs = fetch_conduct_workflow_runs(token, org, conduct_repo, limit=10)

        total_stats = {
            'runs_checked': 0,
            'projects_found': 0,
            'projects_imported': 0,
            'projects_skipped': 0,
            'errors': 0,
        }

        for run in runs:
            if run.get('conclusion') != 'success':
                continue

            total_stats['runs_checked'] += 1
            run_id = run['id']

            # Fetch and parse routing artifact
            routing = fetch_routing_artifact(token, org, conduct_repo, run_id)
            if not routing:
                continue

            # Import PROJECT items
            stats = import_projects_from_routing(routing, run_id, db)
            total_stats['projects_found'] += stats['found']
            total_stats['projects_imported'] += stats['imported']
            total_stats['projects_skipped'] += stats['skipped']
            total_stats['errors'] += stats['errors']

        logger.info(f"Sync complete: {total_stats}")

        return jsonify({
            'status': 'synced',
            **total_stats,
        })

    except Exception as e:
        logger.error(f"Sync failed: {e}")
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
