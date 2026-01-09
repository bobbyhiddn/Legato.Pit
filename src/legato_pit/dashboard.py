"""
Dashboard for Legato.Pit

Displays LEGATO system status, recent jobs, and artifacts.
Uses server-side GitHub API calls with SYSTEM_PAT for reliable access.
"""
import logging
from datetime import datetime

import requests
from flask import Blueprint, render_template, current_app, jsonify

from .core import login_required

logger = logging.getLogger(__name__)

dashboard_bp = Blueprint('dashboard', __name__, url_prefix='/dashboard')

# Repository names
REPOS = {
    'conduct': 'Legato.Conduct',
    'library': 'Legato.Library',
    'listen': 'Legato.Listen'
}


def github_api(endpoint, token=None):
    """Make authenticated GitHub API request."""
    token = token or current_app.config.get('SYSTEM_PAT')

    if not token:
        logger.warning("No GitHub token available for API request")
        return None

    try:
        response = requests.get(
            f'https://api.github.com{endpoint}',
            headers={
                'Authorization': f'Bearer {token}',
                'Accept': 'application/vnd.github+json'
            },
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"GitHub API error for {endpoint}: {e}")
        return None


def get_system_status():
    """Get status of all LEGATO repositories."""
    org = current_app.config['LEGATO_ORG']
    statuses = []

    for key, repo_name in REPOS.items():
        try:
            runs = github_api(f'/repos/{org}/{repo_name}/actions/runs?per_page=1')
            if runs and runs.get('workflow_runs'):
                run = runs['workflow_runs'][0]
                if run['status'] in ('in_progress', 'queued'):
                    status = 'running'
                    text = 'Running'
                elif run['conclusion'] == 'success':
                    status = 'success'
                    text = 'Operational'
                elif run['conclusion'] == 'failure':
                    status = 'error'
                    text = 'Failed'
                else:
                    status = 'warning'
                    text = run['conclusion'] or 'Unknown'
            else:
                status = 'success'
                text = 'Operational'
        except Exception as e:
            logger.error(f"Error fetching status for {repo_name}: {e}")
            status = 'error'
            text = 'Unavailable'

        statuses.append({
            'name': repo_name,
            'status': status,
            'text': text
        })

    return statuses


def get_recent_jobs(limit=5):
    """Get recent transcript processing jobs."""
    org = current_app.config['LEGATO_ORG']
    conduct = current_app.config['CONDUCT_REPO']

    runs = github_api(f'/repos/{org}/{conduct}/actions/workflows/process-transcript.yml/runs?per_page={limit}')

    if not runs or not runs.get('workflow_runs'):
        return []

    jobs = []
    for run in runs['workflow_runs'][:limit]:
        # Determine status
        if run['status'] in ('in_progress', 'queued'):
            status = 'running'
        elif run['conclusion'] == 'success':
            status = 'success'
        elif run['conclusion'] == 'failure':
            status = 'error'
        else:
            status = 'pending'

        jobs.append({
            'id': run['id'],
            'title': run.get('display_title') or run.get('name', 'Transcript Job'),
            'status': status,
            'status_text': run['conclusion'] or run['status'],
            'created_at': run['created_at'],
            'url': run['html_url']
        })

    return jobs


def get_recent_artifacts(limit=5):
    """Get recent artifacts from Library."""
    org = current_app.config['LEGATO_ORG']

    commits = github_api(f'/repos/{org}/Legato.Library/commits?per_page=20')

    if not commits:
        return []

    artifacts = []
    seen_files = set()

    for commit in commits:
        if len(artifacts) >= limit:
            break

        details = github_api(f'/repos/{org}/Legato.Library/commits/{commit["sha"]}')
        if not details or not details.get('files'):
            continue

        for file in details['files']:
            if len(artifacts) >= limit:
                break

            filename = file['filename']
            if (filename.endswith('.md') and
                'README' not in filename and
                filename not in seen_files):

                seen_files.add(filename)
                parts = filename.split('/')
                category = parts[0] if len(parts) > 1 else 'unknown'
                name = parts[-1].replace('.md', '')

                artifacts.append({
                    'name': name,
                    'category': category.rstrip('s'),  # epiphanys -> epiphany
                    'path': filename,
                    'date': commit['commit']['author']['date']
                })

    return artifacts


def get_stats():
    """Get system statistics."""
    org = current_app.config['LEGATO_ORG']

    stats = {
        'transcripts': 0,
        'artifacts': 0,
        'projects': 0
    }

    # Count transcripts (workflow runs)
    runs = github_api(f'/repos/{org}/Legato.Conduct/actions/workflows/process-transcript.yml/runs?per_page=1')
    if runs:
        stats['transcripts'] = runs.get('total_count', 0)

    # Count artifacts (rough estimate from commits)
    commits = github_api(f'/repos/{org}/Legato.Library/commits?per_page=100')
    if commits:
        stats['artifacts'] = len(commits)

    # Count Lab projects
    repos = github_api(f'/users/{org}/repos?per_page=100')
    if repos:
        stats['projects'] = len([r for r in repos if r['name'].startswith('Lab.')])

    return stats


def get_pending_agents():
    """Get pending agents from the queue."""
    from flask import g
    from .rag.database import init_db

    try:
        if 'db_conn' not in g:
            g.db_conn = init_db()
        db = g.db_conn

        rows = db.execute(
            """
            SELECT queue_id, project_name, project_type, title, description,
                   source_transcript, created_at
            FROM agent_queue
            WHERE status = 'pending'
            ORDER BY created_at DESC
            """
        ).fetchall()

        return [dict(row) for row in rows]

    except Exception as e:
        logger.error(f"Error fetching pending agents: {e}")
        return []


@dashboard_bp.route('/')
@login_required
def index():
    """Main dashboard view."""
    return render_template(
        'dashboard.html',
        title='Dashboard',
        system_status=get_system_status(),
        recent_jobs=get_recent_jobs(),
        recent_artifacts=get_recent_artifacts(),
        stats=get_stats(),
        pending_agents=get_pending_agents()
    )


@dashboard_bp.route('/api/status')
@login_required
def api_status():
    """API endpoint for dashboard data (for live updates)."""
    return jsonify({
        'system_status': get_system_status(),
        'recent_jobs': get_recent_jobs(),
        'recent_artifacts': get_recent_artifacts(),
        'stats': get_stats(),
        'updated_at': datetime.now().isoformat()
    })
