"""
SQLite Database Setup for Legato.Pit RAG

Schema includes:
- knowledge_entries: Knowledge artifacts from Library
- project_entries: Project metadata from Lab
- embeddings: Vector embeddings (multi-provider)
- chat_messages: Conversation history
- transcript_hashes: Deduplication fingerprints
"""

import os
import sqlite3
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default database path - can be overridden by FLY_VOLUME_PATH
DEFAULT_DB_DIR = Path(__file__).parent.parent.parent.parent / "data"
FLY_VOLUME_PATH = os.environ.get("FLY_VOLUME_PATH", "/data")


def get_db_path() -> Path:
    """Get the database file path, preferring Fly volume if available."""
    if os.path.exists(FLY_VOLUME_PATH) and os.access(FLY_VOLUME_PATH, os.W_OK):
        return Path(FLY_VOLUME_PATH) / "legato.db"

    # Fallback to local data directory
    DEFAULT_DB_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_DB_DIR / "legato.db"


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Get a database connection with proper settings."""
    path = db_path or get_db_path()
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row

    # Enable foreign keys and WAL mode for better concurrency
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")

    return conn


def init_db(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Initialize the database with all required tables."""
    conn = get_connection(db_path)
    cursor = conn.cursor()

    # Knowledge entries from Library
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            category TEXT,
            content TEXT NOT NULL,
            source_thread TEXT,
            source_transcript TEXT,
            file_path TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Project entries from Lab
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT UNIQUE NOT NULL,
            repo_name TEXT,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT DEFAULT 'active',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Vector embeddings (supports multiple providers)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS embeddings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id INTEGER NOT NULL,
            entry_type TEXT NOT NULL DEFAULT 'knowledge',
            embedding BLOB NOT NULL,
            vector_version TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(entry_id, entry_type, vector_version)
        )
    """)

    # Chat messages with context tracking
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            context_used TEXT,
            model_used TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Chat sessions
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT UNIQUE NOT NULL,
            title TEXT,
            user_id TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Transcript fingerprints for deduplication
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transcript_hashes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash TEXT UNIQUE NOT NULL,
            transcript_id TEXT,
            thread_count INTEGER,
            processed_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Sync tracking
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            commit_sha TEXT,
            entries_synced INTEGER DEFAULT 0,
            status TEXT,
            synced_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Create indexes for common queries
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_category ON knowledge_entries(category)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_entry_id ON knowledge_entries(entry_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_entry ON embeddings(entry_id, entry_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_messages(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_transcript_hash ON transcript_hashes(content_hash)")

    conn.commit()
    logger.info(f"Database initialized at {db_path or get_db_path()}")

    return conn


def backup_to_tigris(conn: sqlite3.Connection, bucket_name: str) -> bool:
    """Backup database to Tigris S3-compatible storage."""
    import boto3
    from datetime import datetime

    try:
        # Get Tigris credentials from environment
        s3 = boto3.client(
            's3',
            endpoint_url=os.environ.get('AWS_ENDPOINT_URL_S3'),
            aws_access_key_id=os.environ.get('AWS_ACCESS_KEY_ID'),
            aws_secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY'),
            region_name=os.environ.get('AWS_REGION', 'auto')
        )

        # Create backup
        db_path = get_db_path()
        backup_path = db_path.parent / f"legato_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"

        # SQLite online backup
        backup_conn = sqlite3.connect(str(backup_path))
        conn.backup(backup_conn)
        backup_conn.close()

        # Upload to Tigris
        key = f"backups/{backup_path.name}"
        s3.upload_file(str(backup_path), bucket_name, key)

        # Clean up local backup
        backup_path.unlink()

        logger.info(f"Database backed up to Tigris: {key}")
        return True

    except Exception as e:
        logger.error(f"Backup to Tigris failed: {e}")
        return False
