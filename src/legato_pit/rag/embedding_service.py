"""
Embedding Service

Core RAG logic including:
- Embedding storage and retrieval
- Similarity search
- Correlation checking
"""

import struct
import logging
import sqlite3
from typing import List, Dict, Optional, Tuple
from threading import Lock

from .embedding_provider import EmbeddingProvider
from .database import get_connection

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Service for managing embeddings and similarity search."""

    def __init__(self, provider: EmbeddingProvider, db_conn: Optional[sqlite3.Connection] = None):
        """Initialize the embedding service.

        Args:
            provider: The embedding provider to use
            db_conn: Optional database connection (creates new if not provided)
        """
        self.provider = provider
        self.conn = db_conn or get_connection()
        self._lock = Lock()

        logger.info(f"EmbeddingService initialized with {provider.model_identifier()}")

    def _serialize_embedding(self, embedding: List[float]) -> bytes:
        """Convert float list to binary blob for storage.

        Uses little-endian float32 format (same as Llore).
        """
        return struct.pack(f'<{len(embedding)}f', *embedding)

    def _deserialize_embedding(self, blob: bytes) -> List[float]:
        """Convert binary blob back to float list."""
        count = len(blob) // 4  # 4 bytes per float32
        return list(struct.unpack(f'<{count}f', blob))

    def store_embedding(
        self,
        entry_id: int,
        entry_type: str,
        embedding: List[float],
    ) -> bool:
        """Store an embedding in the database.

        Args:
            entry_id: The ID of the entry (from knowledge_entries or project_entries)
            entry_type: 'knowledge' or 'project'
            embedding: The embedding vector

        Returns:
            True if successful
        """
        blob = self._serialize_embedding(embedding)
        version = self.provider.model_identifier()

        with self._lock:
            try:
                self.conn.execute(
                    """
                    INSERT INTO embeddings (entry_id, entry_type, embedding, vector_version)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(entry_id, entry_type, vector_version)
                    DO UPDATE SET embedding = excluded.embedding, updated_at = CURRENT_TIMESTAMP
                    """,
                    (entry_id, entry_type, blob, version),
                )
                self.conn.commit()
                logger.debug(f"Stored embedding for {entry_type}:{entry_id}")
                return True

            except sqlite3.Error as e:
                logger.error(f"Failed to store embedding: {e}")
                return False

    def get_embedding(self, entry_id: int, entry_type: str = 'knowledge') -> Optional[List[float]]:
        """Retrieve an embedding from the database.

        Args:
            entry_id: The entry ID
            entry_type: 'knowledge' or 'project'

        Returns:
            The embedding vector or None if not found
        """
        version = self.provider.model_identifier()

        row = self.conn.execute(
            """
            SELECT embedding FROM embeddings
            WHERE entry_id = ? AND entry_type = ? AND vector_version = ?
            """,
            (entry_id, entry_type, version),
        ).fetchone()

        if row:
            return self._deserialize_embedding(row[0])
        return None

    def generate_and_store(
        self,
        entry_id: int,
        entry_type: str,
        text: str,
    ) -> Optional[List[float]]:
        """Generate an embedding for text and store it.

        Args:
            entry_id: The entry ID
            entry_type: 'knowledge' or 'project'
            text: The text to embed

        Returns:
            The embedding vector or None if failed
        """
        try:
            embedding = self.provider.create_embedding(text)
            self.store_embedding(entry_id, entry_type, embedding)
            return embedding
        except Exception as e:
            logger.error(f"Failed to generate embedding: {e}")
            return None

    @staticmethod
    def cosine_similarity(a: List[float], b: List[float]) -> float:
        """Calculate cosine similarity between two vectors.

        Args:
            a: First vector
            b: Second vector

        Returns:
            Similarity score between -1 and 1
        """
        if len(a) != len(b):
            raise ValueError(f"Vector dimension mismatch: {len(a)} vs {len(b)}")

        # Use float64 for precision (like Llore)
        dot_product = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5

        if norm_a == 0 or norm_b == 0:
            return 0.0

        similarity = dot_product / (norm_a * norm_b)

        # Clamp to [-1, 1] to handle floating point errors
        return max(-1.0, min(1.0, similarity))

    def find_similar(
        self,
        query_text: str,
        entry_type: str = 'knowledge',
        limit: int = 10,
        threshold: float = 0.4,
    ) -> List[Dict]:
        """Find entries similar to the query text.

        Args:
            query_text: Text to search for
            entry_type: 'knowledge' or 'project'
            limit: Maximum results to return
            threshold: Minimum similarity score

        Returns:
            List of dicts with entry info and similarity scores
        """
        # Generate query embedding
        try:
            query_embedding = self.provider.create_embedding(query_text)
        except Exception as e:
            logger.error(f"Failed to create query embedding: {e}")
            return []

        version = self.provider.model_identifier()

        # Get all embeddings for comparison
        if entry_type == 'knowledge':
            rows = self.conn.execute(
                """
                SELECT e.entry_id, e.embedding, k.entry_id as eid, k.title, k.category, k.content
                FROM embeddings e
                JOIN knowledge_entries k ON e.entry_id = k.id
                WHERE e.entry_type = 'knowledge' AND e.vector_version = ?
                """,
                (version,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT e.entry_id, e.embedding, p.project_id as eid, p.title, p.status, p.description
                FROM embeddings e
                JOIN project_entries p ON e.entry_id = p.id
                WHERE e.entry_type = 'project' AND e.vector_version = ?
                """,
                (version,),
            ).fetchall()

        # Calculate similarities
        results = []
        for row in rows:
            stored_embedding = self._deserialize_embedding(row['embedding'])
            similarity = self.cosine_similarity(query_embedding, stored_embedding)

            if similarity >= threshold:
                results.append({
                    'id': row['entry_id'],
                    'entry_id': row['eid'],
                    'title': row['title'],
                    'category': row['category'] if entry_type == 'knowledge' else row['status'],
                    'content': row['content'] if entry_type == 'knowledge' else row['description'],
                    'similarity': similarity,
                })

        # Sort by similarity (descending) and limit
        results.sort(key=lambda x: x['similarity'], reverse=True)
        return results[:limit]

    def correlate(
        self,
        title: str,
        content: str,
        threshold_skip: float = 0.90,
        threshold_suggest: float = 0.70,
    ) -> Dict:
        """Check if similar content already exists.

        Args:
            title: Entry title
            content: Entry content
            threshold_skip: Score above this = SKIP (likely duplicate)
            threshold_suggest: Score above this = SUGGEST (needs review)

        Returns:
            Dict with action (CREATE/SUGGEST/SKIP), score, and matches
        """
        # Combine title and content for embedding
        text = f"Title: {title}\n\nContent: {content}"

        similar = self.find_similar(text, limit=5, threshold=threshold_suggest)

        if not similar:
            return {
                'action': 'CREATE',
                'score': 0.0,
                'matches': [],
            }

        top_score = similar[0]['similarity']

        if top_score >= threshold_skip:
            action = 'SKIP'
        elif top_score >= threshold_suggest:
            action = 'SUGGEST'
        else:
            action = 'CREATE'

        return {
            'action': action,
            'score': top_score,
            'matches': [
                {
                    'entry_id': m['entry_id'],
                    'title': m['title'],
                    'similarity': round(m['similarity'], 3),
                }
                for m in similar
            ],
        }

    def get_entries_without_embeddings(self, entry_type: str = 'knowledge') -> List[Tuple[int, str]]:
        """Find entries that don't have embeddings yet.

        Returns:
            List of (id, text) tuples for entries needing embeddings
        """
        version = self.provider.model_identifier()

        if entry_type == 'knowledge':
            rows = self.conn.execute(
                """
                SELECT k.id, k.title, k.content
                FROM knowledge_entries k
                LEFT JOIN embeddings e ON k.id = e.entry_id
                    AND e.entry_type = 'knowledge'
                    AND e.vector_version = ?
                WHERE e.id IS NULL
                """,
                (version,),
            ).fetchall()
            return [(r['id'], f"Title: {r['title']}\n\nContent: {r['content']}") for r in rows]

        else:
            rows = self.conn.execute(
                """
                SELECT p.id, p.title, p.description
                FROM project_entries p
                LEFT JOIN embeddings e ON p.id = e.entry_id
                    AND e.entry_type = 'project'
                    AND e.vector_version = ?
                WHERE e.id IS NULL
                """,
                (version,),
            ).fetchall()
            return [(r['id'], f"Title: {r['title']}\n\nDescription: {r['description'] or ''}") for r in rows]

    def generate_missing_embeddings(self, entry_type: str = 'knowledge', delay: float = 0.1) -> int:
        """Generate embeddings for all entries that don't have them.

        Args:
            entry_type: 'knowledge' or 'project'
            delay: Seconds to wait between API calls

        Returns:
            Number of embeddings generated
        """
        import time

        entries = self.get_entries_without_embeddings(entry_type)
        count = 0

        for entry_id, text in entries:
            try:
                if self.generate_and_store(entry_id, entry_type, text):
                    count += 1
                    logger.info(f"Generated embedding for {entry_type}:{entry_id}")

                if delay > 0:
                    time.sleep(delay)

            except Exception as e:
                logger.error(f"Failed to generate embedding for {entry_type}:{entry_id}: {e}")

        return count
