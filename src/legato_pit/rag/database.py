"""
SQLite Database Setup for Legato.Pit

Split database architecture:
- legato.db: Knowledge entries, embeddings, sync tracking (RAG data)
- agents.db: Agent queue for project spawns
- chat.db: Chat sessions and messages

Archive databases (for future):
- agents_archive.db: Completed/old agent records
- chat_archive.db: Old chat sessions
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


def get_db_dir() -> Path:
    """Get the database directory, preferring Fly volume if available."""
    if os.path.exists(FLY_VOLUME_PATH) and os.access(FLY_VOLUME_PATH, os.W_OK):
        return Path(FLY_VOLUME_PATH)

    # Fallback to local data directory
    DEFAULT_DB_DIR.mkdir(parents=True, exist_ok=True)
    return DEFAULT_DB_DIR


def get_db_path(db_name: str = "legato.db") -> Path:
    """Get path for a specific database file."""
    return get_db_dir() / db_name


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Get a database connection with proper settings."""
    path = db_path or get_db_path()
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row

    # Enable foreign keys and WAL mode for better concurrency
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")

    return conn


# ============ Legato DB (Knowledge/Embeddings) ============

def init_db(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Initialize legato.db with knowledge entries and embeddings.

    This is the main RAG database containing:
    - knowledge_entries: Library knowledge artifacts
    - project_entries: Lab project metadata
    - embeddings: Vector embeddings for similarity search
    - transcript_hashes: Deduplication fingerprints
    - sync_log: Sync tracking
    - pipeline_runs: Pipeline run tracking
    """
    path = db_path or get_db_path("legato.db")
    conn = get_connection(path)
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
            needs_chord INTEGER DEFAULT 0,
            chord_name TEXT,
            chord_scope TEXT,
            chord_id TEXT,
            chord_status TEXT,
            chord_repo TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Add chord columns to existing tables (migration)
    try:
        cursor.execute("ALTER TABLE knowledge_entries ADD COLUMN needs_chord INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # Column already exists
    try:
        cursor.execute("ALTER TABLE knowledge_entries ADD COLUMN chord_name TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE knowledge_entries ADD COLUMN chord_scope TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE knowledge_entries ADD COLUMN chord_id TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE knowledge_entries ADD COLUMN chord_status TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        cursor.execute("ALTER TABLE knowledge_entries ADD COLUMN chord_repo TEXT")
    except sqlite3.OperationalError:
        pass

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

    # Pipeline run tracking
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            details TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id, stage)
        )
    """)

    # Create indexes for common queries
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_category ON knowledge_entries(category)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_entry_id ON knowledge_entries(entry_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_needs_chord ON knowledge_entries(needs_chord, chord_status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_entry ON embeddings(entry_id, entry_type)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_transcript_hash ON transcript_hashes(content_hash)")

    conn.commit()
    logger.info(f"Legato database initialized at {path}")

    return conn


# ============ Agents DB ============

def init_agents_db(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Initialize agents.db for agent queue management.

    Contains:
    - agent_queue: Pending project spawns awaiting approval
    """
    path = db_path or get_db_path("agents.db")
    conn = get_connection(path)
    cursor = conn.cursor()

    # Agent queue for pending project spawns
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agent_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            queue_id TEXT UNIQUE NOT NULL,
            project_name TEXT NOT NULL,
            project_type TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            signal_json TEXT NOT NULL,
            tasker_body TEXT NOT NULL,
            source_transcript TEXT,
            related_entry_id TEXT,
            status TEXT DEFAULT 'pending',
            approved_by TEXT,
            approved_at DATETIME,
            spawn_result TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Create indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_queue_status ON agent_queue(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_agent_queue_source ON agent_queue(source_transcript)")

    # Sync history to track processed workflow runs (persists even when queue is cleared)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sync_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            item_id TEXT NOT NULL,
            processed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id, item_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_history_run ON sync_history(run_id)")

    conn.commit()
    logger.info(f"Agents database initialized at {path}")

    return conn


# ============ Chat DB ============

def init_chat_db(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Initialize chat.db for chat sessions and messages.

    Contains:
    - chat_sessions: User chat sessions
    - chat_messages: Individual messages with context
    """
    path = db_path or get_db_path("chat.db")
    conn = get_connection(path)
    cursor = conn.cursor()

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

    # Chat messages with context tracking
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            context_used TEXT,
            model_used TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES chat_sessions(session_id)
        )
    """)

    # Create indexes
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_messages(session_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_sessions_user ON chat_sessions(user_id)")

    conn.commit()
    logger.info(f"Chat database initialized at {path}")

    return conn


def backup_to_tigris(conn: sqlite3.Connection, bucket_name: str, db_name: str = "legato") -> bool:
    """Backup a single database to Tigris S3-compatible storage.

    Args:
        conn: Database connection to backup
        bucket_name: Tigris bucket name
        db_name: Name prefix for backup file (e.g., 'legato', 'agents', 'chat')
    """
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
        db_dir = get_db_dir()
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = db_dir / f"{db_name}_backup_{timestamp}.db"

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


def backup_all_to_tigris(bucket_name: str) -> dict:
    """Backup all databases to Tigris S3-compatible storage.

    Returns:
        Dict with backup status for each database
    """
    results = {}

    # Backup each database
    for db_name, init_func in [
        ("legato", init_db),
        ("agents", init_agents_db),
        ("chat", init_chat_db),
    ]:
        try:
            conn = init_func()
            success = backup_to_tigris(conn, bucket_name, db_name)
            results[db_name] = {"success": success}
        except Exception as e:
            results[db_name] = {"success": False, "error": str(e)}

    return results
