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


def get_file_content(
    repo: str,
    path: str,
    token: str,
    branch: str = "main",
) -> Optional[str]:
    """Get file content from GitHub.

    Args:
        repo: Repository in "owner/repo" format
        path: File path within repo
        token: GitHub PAT
        branch: Branch name

    Returns:
        File content as string, or None if not found
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

    if response.status_code == 404:
        return None

    response.raise_for_status()
    data = response.json()

    # Decode base64 content
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content


def delete_file(
    repo: str,
    path: str,
    message: str,
    token: str,
    branch: str = "main",
) -> dict:
    """Delete a file from GitHub.

    Args:
        repo: Repository in "owner/repo" format
        path: File path within repo
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

    # Delete via Contents API
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    response = requests.delete(
        url,
        json={
            "message": message,
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
    logger.info(f"Deleted {path} from {repo}: {result['commit']['sha'][:7]}")
    return result


def create_file(
    repo: str,
    path: str,
    content: str,
    message: str,
    token: str,
    branch: str = "main",
) -> dict:
    """Create a new file on GitHub (fails if exists).

    Args:
        repo: Repository in "owner/repo" format
        path: File path within repo
        content: File content (plain text)
        message: Commit message
        token: GitHub PAT
        branch: Branch name

    Returns:
        Dict with commit info from GitHub API

    Raises:
        requests.RequestException on API errors
    """
    # Encode content to base64
    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")

    # Create via Contents API (no sha = create new)
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    response = requests.put(
        url,
        json={
            "message": message,
            "content": encoded,
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
    logger.info(f"Created {path} in {repo}: {result['commit']['sha'][:7]}")
    return result
