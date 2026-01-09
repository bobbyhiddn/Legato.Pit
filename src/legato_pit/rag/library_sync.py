"""
Library Sync

Synchronizes content from Legato.Library GitHub repository into the local SQLite database.
Supports both GitHub API fetching and local filesystem sync.
"""

import os
import re
import logging
import hashlib
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from datetime import datetime

import requests

logger = logging.getLogger(__name__)


def parse_markdown_frontmatter(content: str) -> Tuple[Dict, str]:
    """Parse YAML frontmatter from markdown content.

    Args:
        content: Raw markdown file content

    Returns:
        Tuple of (frontmatter dict, body content)
    """
    frontmatter = {}
    body = content

    # Check for YAML frontmatter (--- delimited)
    if content.startswith('---'):
        parts = content.split('---', 2)
        if len(parts) >= 3:
            try:
                # Simple YAML parsing (key: value)
                for line in parts[1].strip().split('\n'):
                    if ':' in line:
                        key, value = line.split(':', 1)
                        frontmatter[key.strip()] = value.strip().strip('"\'')
                body = parts[2].strip()
            except Exception as e:
                logger.warning(f"Failed to parse frontmatter: {e}")

    return frontmatter, body


def extract_title_from_content(content: str, filename: str) -> str:
    """Extract title from markdown content or filename.

    Args:
        content: Markdown content
        filename: Original filename

    Returns:
        Extracted title
    """
    # Try to find first H1 heading
    match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
    if match:
        return match.group(1).strip()

    # Fall back to filename
    name = Path(filename).stem
    # Remove date prefix if present (e.g., 2026-01-08-title)
    name = re.sub(r'^\d{4}-\d{2}-\d{2}-', '', name)
    # Convert kebab-case to title case
    return name.replace('-', ' ').title()


def categorize_from_path(path: str) -> str:
    """Determine category from file path.

    Args:
        path: File path (e.g., "concepts/file.md")

    Returns:
        Category string
    """
    parts = Path(path).parts
    if len(parts) > 0:
        category = parts[0].lower()
        # Map folder names to categories
        category_map = {
            'concepts': 'concepts',
            'epiphanys': 'epiphanies',
            'epiphanies': 'epiphanies',
            'reflections': 'reflections',
            'worklogs': 'worklogs',
            'procedures': 'procedures',
            'references': 'references',
        }
        return category_map.get(category, category)
    return 'general'


def generate_entry_id(category: str, title: str) -> str:
    """Generate a unique entry ID.

    Args:
        category: Entry category
        title: Entry title

    Returns:
        Entry ID like "kb-a1b2c3d4"
    """
    # Create a hash from category + title
    content = f"{category}:{title}"
    hash_val = hashlib.md5(content.encode()).hexdigest()[:8]
    return f"kb-{hash_val}"


class LibrarySync:
    """Synchronizes Legato.Library content to SQLite database."""

    def __init__(self, db_conn, embedding_service=None):
        """Initialize the sync service.

        Args:
            db_conn: SQLite database connection
            embedding_service: Optional EmbeddingService for generating embeddings
        """
        self.conn = db_conn
        self.embedding_service = embedding_service

    def sync_from_github(
        self,
        repo: str = "bobbyhiddn/Legato.Library",
        token: Optional[str] = None,
        branch: str = "main",
    ) -> Dict:
        """Sync content from GitHub repository.

        Args:
            repo: GitHub repo in "owner/repo" format
            token: GitHub PAT for API access
            branch: Branch to sync from

        Returns:
            Dict with sync statistics
        """
        token = token or os.environ.get('SYSTEM_PAT')
        if not token:
            raise ValueError("GitHub token required for sync")

        headers = {
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
        }

        stats = {
            'files_found': 0,
            'entries_created': 0,
            'entries_updated': 0,
            'errors': 0,
            'embeddings_generated': 0,
        }

        try:
            # Get repository tree
            tree_url = f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1"
            response = requests.get(tree_url, headers=headers, timeout=30)
            response.raise_for_status()
            tree_data = response.json()

            # Filter for markdown files
            md_files = [
                item for item in tree_data.get('tree', [])
                if item['type'] == 'blob' and item['path'].endswith('.md')
                and not item['path'].startswith('.')
                and item['path'] != 'README.md'
            ]

            stats['files_found'] = len(md_files)
            logger.info(f"Found {len(md_files)} markdown files in {repo}")

            # Process each file
            for item in md_files:
                try:
                    result = self._process_github_file(
                        repo, item['path'], item['sha'], headers
                    )
                    if result == 'created':
                        stats['entries_created'] += 1
                    elif result == 'updated':
                        stats['entries_updated'] += 1
                except Exception as e:
                    logger.error(f"Error processing {item['path']}: {e}")
                    stats['errors'] += 1

            # Generate embeddings for new entries
            if self.embedding_service:
                stats['embeddings_generated'] = self.embedding_service.generate_missing_embeddings(
                    'knowledge', delay=0.1
                )

            # Log sync
            self._log_sync(repo, branch, stats)

            return stats

        except requests.RequestException as e:
            logger.error(f"GitHub API error: {e}")
            raise

    def _process_github_file(
        self,
        repo: str,
        path: str,
        sha: str,
        headers: Dict,
    ) -> str:
        """Process a single file from GitHub.

        Returns:
            'created', 'updated', or 'skipped'
        """
        # Fetch file content
        content_url = f"https://api.github.com/repos/{repo}/contents/{path}"
        response = requests.get(content_url, headers=headers, timeout=30)
        response.raise_for_status()

        file_data = response.json()
        import base64
        content = base64.b64decode(file_data['content']).decode('utf-8')

        # Parse frontmatter and content
        frontmatter, body = parse_markdown_frontmatter(content)

        # Extract metadata
        title = frontmatter.get('title') or extract_title_from_content(body, path)
        category = frontmatter.get('category') or categorize_from_path(path)
        # Always generate entry_id from hash to ensure URL-safe format
        entry_id = generate_entry_id(category, title)

        # Check if entry exists by file_path (more reliable than entry_id)
        existing = self.conn.execute(
            "SELECT id, entry_id FROM knowledge_entries WHERE file_path = ?",
            (path,)
        ).fetchone()

        if existing:
            # Update existing entry (including entry_id if changed)
            self.conn.execute(
                """
                UPDATE knowledge_entries
                SET entry_id = ?, title = ?, category = ?, content = ?, updated_at = CURRENT_TIMESTAMP
                WHERE file_path = ?
                """,
                (entry_id, title, category, body, path)
            )
            self.conn.commit()
            logger.debug(f"Updated: {entry_id} - {title}")
            return 'updated'
        else:
            # Create new entry
            self.conn.execute(
                """
                INSERT INTO knowledge_entries (entry_id, title, category, content, file_path)
                VALUES (?, ?, ?, ?, ?)
                """,
                (entry_id, title, category, body, path)
            )
            self.conn.commit()
            logger.info(f"Created: {entry_id} - {title}")
            return 'created'

    def sync_from_filesystem(self, library_path: str) -> Dict:
        """Sync content from local filesystem.

        Args:
            library_path: Path to Legato.Library directory

        Returns:
            Dict with sync statistics
        """
        library_path = Path(library_path)
        if not library_path.exists():
            raise ValueError(f"Library path does not exist: {library_path}")

        stats = {
            'files_found': 0,
            'entries_created': 0,
            'entries_updated': 0,
            'errors': 0,
            'embeddings_generated': 0,
        }

        # Find all markdown files
        md_files = list(library_path.glob('**/*.md'))
        md_files = [f for f in md_files if f.name != 'README.md' and not f.name.startswith('.')]

        stats['files_found'] = len(md_files)
        logger.info(f"Found {len(md_files)} markdown files in {library_path}")

        for file_path in md_files:
            try:
                result = self._process_local_file(file_path, library_path)
                if result == 'created':
                    stats['entries_created'] += 1
                elif result == 'updated':
                    stats['entries_updated'] += 1
            except Exception as e:
                logger.error(f"Error processing {file_path}: {e}")
                stats['errors'] += 1

        # Generate embeddings
        if self.embedding_service:
            stats['embeddings_generated'] = self.embedding_service.generate_missing_embeddings(
                'knowledge', delay=0.1
            )

        # Log sync
        self._log_sync(str(library_path), 'filesystem', stats)

        return stats

    def _process_local_file(self, file_path: Path, base_path: Path) -> str:
        """Process a single local file.

        Returns:
            'created', 'updated', or 'skipped'
        """
        content = file_path.read_text(encoding='utf-8')
        relative_path = str(file_path.relative_to(base_path))

        # Parse frontmatter and content
        frontmatter, body = parse_markdown_frontmatter(content)

        # Extract metadata
        title = frontmatter.get('title') or extract_title_from_content(body, file_path.name)
        category = frontmatter.get('category') or categorize_from_path(relative_path)
        # Always generate entry_id from hash to ensure URL-safe format
        entry_id = generate_entry_id(category, title)

        # Check if entry exists by file_path (more reliable than entry_id)
        existing = self.conn.execute(
            "SELECT id, entry_id FROM knowledge_entries WHERE file_path = ?",
            (relative_path,)
        ).fetchone()

        if existing:
            self.conn.execute(
                """
                UPDATE knowledge_entries
                SET entry_id = ?, title = ?, category = ?, content = ?, updated_at = CURRENT_TIMESTAMP
                WHERE file_path = ?
                """,
                (entry_id, title, category, body, relative_path)
            )
            self.conn.commit()
            logger.debug(f"Updated: {entry_id} - {title}")
            return 'updated'
        else:
            self.conn.execute(
                """
                INSERT INTO knowledge_entries (entry_id, title, category, content, file_path)
                VALUES (?, ?, ?, ?, ?)
                """,
                (entry_id, title, category, body, relative_path)
            )
            self.conn.commit()
            logger.info(f"Created: {entry_id} - {title}")
            return 'created'

    def _log_sync(self, source: str, branch: str, stats: Dict):
        """Log sync operation to database."""
        self.conn.execute(
            """
            INSERT INTO sync_log (source, commit_sha, entries_synced, status)
            VALUES (?, ?, ?, ?)
            """,
            (
                source,
                branch,
                stats['entries_created'] + stats['entries_updated'],
                'success' if stats['errors'] == 0 else 'partial'
            )
        )
        self.conn.commit()

    def get_sync_status(self) -> Dict:
        """Get the latest sync status."""
        row = self.conn.execute(
            """
            SELECT source, commit_sha, entries_synced, status, synced_at
            FROM sync_log
            ORDER BY synced_at DESC
            LIMIT 1
            """
        ).fetchone()

        if row:
            return dict(row)
        return {'status': 'never_synced'}
