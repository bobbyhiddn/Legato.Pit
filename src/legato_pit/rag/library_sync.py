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
                        value = value.strip().strip('"\'')
                        # Parse boolean values
                        if value.lower() == 'true':
                            value = True
                        elif value.lower() == 'false':
                            value = False
                        elif value.lower() == 'null':
                            value = None
                        frontmatter[key.strip()] = value
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

    Normalizes folder names to canonical singular category names.
    Handles both old plural folders and new singular folders.

    Args:
        path: File path (e.g., "concept/file.md" or "concepts/file.md")

    Returns:
        Canonical category string (singular form)
    """
    parts = Path(path).parts
    if len(parts) > 0:
        folder = parts[0].lower()
        # Map folder names to canonical singular category names
        # Handles typos, plurals, and legacy folder names
        category_map = {
            # Singular (canonical)
            'concept': 'concept',
            'epiphany': 'epiphany',
            'reflection': 'reflection',
            'glimmer': 'glimmer',
            'reminder': 'reminder',
            'worklog': 'worklog',
            'tech-thought': 'tech-thought',
            'research-topic': 'research-topic',
            'theology': 'theology',
            # Plural (legacy)
            'concepts': 'concept',
            'epiphanies': 'epiphany',
            'reflections': 'reflection',
            'glimmers': 'glimmer',
            'reminders': 'reminder',
            'worklogs': 'worklog',
            'tech-thoughts': 'tech-thought',
            'research-topics': 'research-topic',
            'theologies': 'theology',
            # Typos
            'epiphanys': 'epiphany',
            'theologys': 'theology',
            'tech-thoughtss': 'tech-thought',
            'research-topicss': 'research-topic',
            # Other
            'procedures': 'procedure',
            'procedure': 'procedure',
            'references': 'reference',
            'reference': 'reference',
        }
        return category_map.get(folder, folder)
    return 'general'


def compute_content_hash(content: str) -> str:
    """Compute a stable hash of content for deduplication and integrity.

    Args:
        content: The markdown body content (after frontmatter)

    Returns:
        First 16 characters of SHA256 hash
    """
    # Normalize: strip whitespace, lowercase for comparison stability
    normalized = content.strip()
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def generate_slug(title: str) -> str:
    """Generate a URL-safe slug from a title.

    Args:
        title: The note title

    Returns:
        Slug like "my-note-title"
    """
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower())[:50].strip('-')
    return slug or 'untitled'


def generate_entry_id(category: str, title: str) -> str:
    """Generate a canonical entry ID in the standard format.

    Args:
        category: Entry category (singular form like 'concept')
        title: Entry title

    Returns:
        Entry ID like "library.concept.my-note-title"
    """
    slug = generate_slug(title)
    # Use singular category form for consistency
    return f"library.{category}.{slug}"


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
            # Get default branch from repo info
            repo_info_url = f"https://api.github.com/repos/{repo}"
            repo_response = requests.get(repo_info_url, headers=headers, timeout=10)
            if repo_response.ok:
                repo_data = repo_response.json()
                branch = repo_data.get('default_branch', branch)
                # Check if repo is empty (no commits)
                if repo_data.get('size', 0) == 0:
                    logger.warning(f"Repository {repo} appears to be empty (no commits)")
                    stats['errors'] = 0
                    stats['message'] = 'Repository is empty - please add initial content'
                    return stats

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

            # Generate embeddings only if we created/updated entries
            if self.embedding_service and (stats['entries_created'] > 0 or stats['entries_updated'] > 0):
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

        # Compute content hash for integrity/deduplication
        content_hash = compute_content_hash(body)

        # Prefer frontmatter ID if present, otherwise generate canonical ID
        # This ensures IDs remain stable once set
        frontmatter_id = frontmatter.get('id')
        if frontmatter_id and isinstance(frontmatter_id, str) and frontmatter_id.strip():
            entry_id = frontmatter_id.strip()
        else:
            # Generate new canonical format ID
            entry_id = generate_entry_id(category, title)

        # Extract chord fields
        needs_chord = 1 if frontmatter.get('needs_chord') else 0
        chord_name = frontmatter.get('chord_name')
        chord_scope = frontmatter.get('chord_scope')
        chord_id = frontmatter.get('chord_id')
        chord_status = frontmatter.get('chord_status')
        chord_repo = frontmatter.get('chord_repo')

        # Extract source transcript for tracking
        source_transcript = frontmatter.get('source_transcript')

        # Extract task fields from frontmatter
        task_status = frontmatter.get('task_status')
        due_date = frontmatter.get('due_date')

        # Validate task_status if present
        valid_task_statuses = {'pending', 'in_progress', 'blocked', 'done'}
        if task_status and task_status not in valid_task_statuses:
            logger.warning(f"Invalid task_status '{task_status}' in {path}, ignoring")
            task_status = None

        # Extract topic tags - stored as JSON strings
        import json
        domain_tags_raw = frontmatter.get('domain_tags')
        key_phrases_raw = frontmatter.get('key_phrases')

        # Parse JSON arrays or keep as-is if already parsed
        if isinstance(domain_tags_raw, str) and domain_tags_raw.startswith('['):
            try:
                domain_tags = json.dumps(json.loads(domain_tags_raw))
            except json.JSONDecodeError:
                domain_tags = domain_tags_raw
        elif isinstance(domain_tags_raw, list):
            domain_tags = json.dumps(domain_tags_raw)
        else:
            domain_tags = None

        if isinstance(key_phrases_raw, str) and key_phrases_raw.startswith('['):
            try:
                key_phrases = json.dumps(json.loads(key_phrases_raw))
            except json.JSONDecodeError:
                key_phrases = key_phrases_raw
        elif isinstance(key_phrases_raw, list):
            key_phrases = json.dumps(key_phrases_raw)
        else:
            key_phrases = None

        # Check if entry exists by file_path (more reliable than entry_id)
        existing = self.conn.execute(
            "SELECT id, entry_id FROM knowledge_entries WHERE file_path = ?",
            (path,)
        ).fetchone()

        if existing:
            # Update existing entry (including entry_id if changed)
            # Chord status logic:
            # - If frontmatter sets needs_chord: false → clear chord fields (no chord needed)
            # - If frontmatter sets needs_chord: true AND has explicit chord_status → use frontmatter
            # - If frontmatter sets needs_chord: true AND no chord_status → check DB, preserve 'pending' or 'active'
            #   (GitHub frontmatter update may not have propagated yet after agent approval)
            existing_data = self.conn.execute(
                "SELECT chord_status, chord_repo, chord_id FROM knowledge_entries WHERE file_path = ?",
                (path,)
            ).fetchone()

            if needs_chord:
                # Entry needs a chord
                if chord_status:
                    # Frontmatter explicitly sets chord_status - use it
                    final_chord_status = chord_status
                    final_chord_repo = chord_repo
                    final_chord_id = chord_id
                elif existing_data and existing_data['chord_status'] in ('pending', 'active', 'rejected'):
                    # Preserve 'pending', 'active', or 'rejected' status from DB
                    # (GitHub frontmatter may not have propagated yet after agent approval/rejection)
                    final_chord_status = existing_data['chord_status']
                    final_chord_repo = existing_data['chord_repo']
                    final_chord_id = existing_data['chord_id']
                else:
                    # No frontmatter chord_status, not pending/active/rejected in DB
                    # Reset to null so it can be queued
                    final_chord_status = None
                    final_chord_repo = None
                    final_chord_id = None
            else:
                # Entry doesn't need a chord - clear chord fields
                final_chord_status = None
                final_chord_repo = None
                final_chord_id = None

            self.conn.execute(
                """
                UPDATE knowledge_entries
                SET entry_id = ?, title = ?, category = ?, content = ?,
                    needs_chord = ?, chord_name = ?, chord_scope = ?,
                    chord_id = ?, chord_status = ?, chord_repo = ?,
                    domain_tags = ?, key_phrases = ?, source_transcript = ?,
                    task_status = ?, due_date = ?, content_hash = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE file_path = ?
                """,
                (entry_id, title, category, body,
                 needs_chord, chord_name, chord_scope,
                 final_chord_id, final_chord_status, final_chord_repo,
                 domain_tags, key_phrases, source_transcript,
                 task_status, due_date, content_hash, path)
            )
            self.conn.commit()
            logger.debug(f"Updated: {entry_id} - {title}" + (f" [task:{task_status}]" if task_status else ""))
            return 'updated'
        else:
            # Create new entry
            self.conn.execute(
                """
                INSERT INTO knowledge_entries
                (entry_id, title, category, content, file_path,
                 needs_chord, chord_name, chord_scope, chord_id, chord_status, chord_repo,
                 domain_tags, key_phrases, source_transcript,
                 task_status, due_date, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (entry_id, title, category, body, path,
                 needs_chord, chord_name, chord_scope, chord_id, chord_status, chord_repo,
                 domain_tags, key_phrases, source_transcript,
                 task_status, due_date, content_hash)
            )
            self.conn.commit()
            logger.info(f"Created: {entry_id} - {title}" + (" [needs_chord]" if needs_chord else "") + (f" [task:{task_status}]" if task_status else ""))
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

        # Generate embeddings only if we created/updated entries
        if self.embedding_service and (stats['entries_created'] > 0 or stats['entries_updated'] > 0):
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

        # Compute content hash for integrity/deduplication
        content_hash = compute_content_hash(body)

        # Prefer frontmatter ID if present, otherwise generate canonical ID
        frontmatter_id = frontmatter.get('id')
        if frontmatter_id and isinstance(frontmatter_id, str) and frontmatter_id.strip():
            entry_id = frontmatter_id.strip()
        else:
            entry_id = generate_entry_id(category, title)

        # Extract chord fields
        needs_chord = 1 if frontmatter.get('needs_chord') else 0
        chord_name = frontmatter.get('chord_name')
        chord_scope = frontmatter.get('chord_scope')
        chord_id = frontmatter.get('chord_id')
        chord_status = frontmatter.get('chord_status')
        chord_repo = frontmatter.get('chord_repo')

        # Extract source transcript for tracking
        source_transcript = frontmatter.get('source_transcript')

        # Extract task fields from frontmatter
        task_status = frontmatter.get('task_status')
        due_date = frontmatter.get('due_date')

        # Validate task_status if present
        valid_task_statuses = {'pending', 'in_progress', 'blocked', 'done'}
        if task_status and task_status not in valid_task_statuses:
            logger.warning(f"Invalid task_status '{task_status}' in {relative_path}, ignoring")
            task_status = None

        # Extract topic tags - stored as JSON strings
        import json
        domain_tags_raw = frontmatter.get('domain_tags')
        key_phrases_raw = frontmatter.get('key_phrases')

        if isinstance(domain_tags_raw, str) and domain_tags_raw.startswith('['):
            try:
                domain_tags = json.dumps(json.loads(domain_tags_raw))
            except json.JSONDecodeError:
                domain_tags = domain_tags_raw
        elif isinstance(domain_tags_raw, list):
            domain_tags = json.dumps(domain_tags_raw)
        else:
            domain_tags = None

        if isinstance(key_phrases_raw, str) and key_phrases_raw.startswith('['):
            try:
                key_phrases = json.dumps(json.loads(key_phrases_raw))
            except json.JSONDecodeError:
                key_phrases = key_phrases_raw
        elif isinstance(key_phrases_raw, list):
            key_phrases = json.dumps(key_phrases_raw)
        else:
            key_phrases = None

        # Check if entry exists by file_path (more reliable than entry_id)
        existing = self.conn.execute(
            "SELECT id, entry_id FROM knowledge_entries WHERE file_path = ?",
            (relative_path,)
        ).fetchone()

        if existing:
            # Same chord status logic as GitHub sync
            existing_data = self.conn.execute(
                "SELECT chord_status, chord_repo, chord_id FROM knowledge_entries WHERE file_path = ?",
                (relative_path,)
            ).fetchone()

            if needs_chord:
                if chord_status:
                    final_chord_status = chord_status
                    final_chord_repo = chord_repo
                    final_chord_id = chord_id
                elif existing_data and existing_data['chord_status'] in ('pending', 'active', 'rejected'):
                    # Preserve 'pending', 'active', or 'rejected' status from DB
                    final_chord_status = existing_data['chord_status']
                    final_chord_repo = existing_data['chord_repo']
                    final_chord_id = existing_data['chord_id']
                else:
                    final_chord_status = None
                    final_chord_repo = None
                    final_chord_id = None
            else:
                final_chord_status = None
                final_chord_repo = None
                final_chord_id = None

            self.conn.execute(
                """
                UPDATE knowledge_entries
                SET entry_id = ?, title = ?, category = ?, content = ?,
                    needs_chord = ?, chord_name = ?, chord_scope = ?,
                    chord_id = ?, chord_status = ?, chord_repo = ?,
                    domain_tags = ?, key_phrases = ?, source_transcript = ?,
                    task_status = ?, due_date = ?, content_hash = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE file_path = ?
                """,
                (entry_id, title, category, body,
                 needs_chord, chord_name, chord_scope,
                 final_chord_id, final_chord_status, final_chord_repo,
                 domain_tags, key_phrases, source_transcript,
                 task_status, due_date, content_hash, relative_path)
            )
            self.conn.commit()
            logger.debug(f"Updated: {entry_id} - {title}" + (f" [task:{task_status}]" if task_status else ""))
            return 'updated'
        else:
            self.conn.execute(
                """
                INSERT INTO knowledge_entries
                (entry_id, title, category, content, file_path,
                 needs_chord, chord_name, chord_scope, chord_id, chord_status, chord_repo,
                 domain_tags, key_phrases, source_transcript,
                 task_status, due_date, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (entry_id, title, category, body, relative_path,
                 needs_chord, chord_name, chord_scope, chord_id, chord_status, chord_repo,
                 domain_tags, key_phrases, source_transcript,
                 task_status, due_date, content_hash)
            )
            self.conn.commit()
            logger.info(f"Created: {entry_id} - {title}" + (" [needs_chord]" if needs_chord else "") + (f" [task:{task_status}]" if task_status else ""))
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
