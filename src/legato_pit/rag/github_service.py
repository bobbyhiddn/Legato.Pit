"""
GitHub Service

Handles GitHub API operations for committing files.
"""

import os
import base64
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)


def get_file_sha(
    repo: str,
    path: str,
    token: str,
    branch: str = "main",
) -> str:
    """Get current SHA of a file from GitHub.

    Args:
        repo: Repository in "owner/repo" format
        path: File path within repo
        token: GitHub PAT
        branch: Branch name

    Returns:
        SHA string of the file

    Raises:
        requests.RequestException on API errors
    """
    url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
    response = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=10,
    )
    response.raise_for_status()
    return response.json()["sha"]


def commit_file(
    repo: str,
    path: str,
    content: str,
    message: str,
    token: str,
    branch: str = "main",
) -> dict:
    """Commit a file to GitHub.

    Args:
        repo: Repository in "owner/repo" format
        path: File path within repo
        content: New file content (plain text)
        message: Commit message
        token: GitHub PAT
        branch: Branch name

    Returns:
        Dict with commit info from GitHub API

    Raises:
        requests.RequestException on API errors
    """
    # Get current SHA
    sha = get_file_sha(repo, path, token, branch)

    # Encode content to base64
    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")

    # Commit via Contents API
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    response = requests.put(
        url,
        json={
            "message": message,
            "content": encoded,
            "sha": sha,
            "branch": branch,
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=15,
    )
    response.raise_for_status()

    result = response.json()
    logger.info(f"Committed {path} to {repo}: {result['commit']['sha'][:7]}")
    return result
