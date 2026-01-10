"""
Chords Blueprint for Legato.Pit

Tracks Chord repositories spawned by agents from Notes.
Fetches repos with 'legato-chord' topic from GitHub.
"""

import logging

import requests
from flask import Blueprint, render_template, jsonify, current_app

from .core import login_required

logger = logging.getLogger(__name__)

chords_bp = Blueprint('chords', __name__, url_prefix='/chords')


def fetch_chord_repos(token: str, org: str) -> list[dict]:
    """Fetch all Chord repos from GitHub with legato-chord topic.

    Args:
        token: GitHub PAT
        org: GitHub organization

    Returns:
        List of repo data dicts
    """
    repos = []

    # Search for repos with legato-chord topic in the org
    search_url = "https://api.github.com/search/repositories"
    params = {
        "q": f"org:{org} topic:legato-chord",
        "sort": "created",
        "order": "desc",
        "per_page": 50,
    }

    try:
        response = requests.get(
            search_url,
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()

        for repo in data.get("items", []):
            repos.append({
                "name": repo["name"],
                "full_name": repo["full_name"],
                "description": repo["description"],
                "html_url": repo["html_url"],
                "created_at": repo["created_at"],
                "updated_at": repo["updated_at"],
                "open_issues_count": repo["open_issues_count"],
                "topics": repo.get("topics", []),
                "default_branch": repo.get("default_branch", "main"),
            })

    except requests.RequestException as e:
        logger.error(f"Failed to fetch chord repos: {e}")

    return repos


def fetch_repo_details(token: str, repo_full_name: str) -> dict:
    """Fetch detailed info for a specific repo including issues and PRs.

    Args:
        token: GitHub PAT
        repo_full_name: Full repo name (org/repo)

    Returns:
        Dict with repo details, issues, PRs
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    details = {
        "issues": [],
        "pull_requests": [],
        "recent_commits": [],
    }

    try:
        # Fetch open issues (not PRs)
        issues_resp = requests.get(
            f"https://api.github.com/repos/{repo_full_name}/issues",
            params={"state": "open", "per_page": 10},
            headers=headers,
            timeout=10,
        )
        if issues_resp.ok:
            for issue in issues_resp.json():
                if "pull_request" not in issue:
                    details["issues"].append({
                        "number": issue["number"],
                        "title": issue["title"],
                        "state": issue["state"],
                        "html_url": issue["html_url"],
                        "created_at": issue["created_at"],
                        "labels": [l["name"] for l in issue.get("labels", [])],
                        "assignee": issue["assignee"]["login"] if issue.get("assignee") else None,
                    })

        # Fetch open PRs
        prs_resp = requests.get(
            f"https://api.github.com/repos/{repo_full_name}/pulls",
            params={"state": "open", "per_page": 10},
            headers=headers,
            timeout=10,
        )
        if prs_resp.ok:
            for pr in prs_resp.json():
                details["pull_requests"].append({
                    "number": pr["number"],
                    "title": pr["title"],
                    "state": pr["state"],
                    "html_url": pr["html_url"],
                    "created_at": pr["created_at"],
                    "user": pr["user"]["login"],
                    "draft": pr.get("draft", False),
                })

        # Fetch recent commits
        commits_resp = requests.get(
            f"https://api.github.com/repos/{repo_full_name}/commits",
            params={"per_page": 5},
            headers=headers,
            timeout=10,
        )
        if commits_resp.ok:
            for commit in commits_resp.json():
                details["recent_commits"].append({
                    "sha": commit["sha"][:7],
                    "message": commit["commit"]["message"].split("\n")[0][:80],
                    "author": commit["commit"]["author"]["name"],
                    "date": commit["commit"]["author"]["date"],
                    "html_url": commit["html_url"],
                })

    except requests.RequestException as e:
        logger.error(f"Failed to fetch repo details for {repo_full_name}: {e}")

    return details


@chords_bp.route('/')
@login_required
def index():
    """Chords overview - list all Chord repos."""
    token = current_app.config.get('SYSTEM_PAT')
    org = current_app.config.get('LEGATO_ORG', 'bobbyhiddn')

    repos = []
    if token:
        repos = fetch_chord_repos(token, org)

    return render_template('chords.html', repos=repos)


@chords_bp.route('/api/repos')
@login_required
def api_list_repos():
    """API endpoint to list Chord repos."""
    token = current_app.config.get('SYSTEM_PAT')
    org = current_app.config.get('LEGATO_ORG', 'bobbyhiddn')

    if not token:
        return jsonify({'error': 'SYSTEM_PAT not configured'}), 500

    repos = fetch_chord_repos(token, org)

    return jsonify({
        'repos': repos,
        'count': len(repos),
    })


@chords_bp.route('/api/repo/<path:repo_name>')
@login_required
def api_repo_details(repo_name: str):
    """API endpoint to get details for a specific repo."""
    token = current_app.config.get('SYSTEM_PAT')

    if not token:
        return jsonify({'error': 'SYSTEM_PAT not configured'}), 500

    details = fetch_repo_details(token, repo_name)

    return jsonify(details)
