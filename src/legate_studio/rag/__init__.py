"""
Legato.Pit RAG (Retrieval-Augmented Generation) Module

Provides SQLite-based vector storage and semantic search for the LEGATO knowledge base.
"""

from .context_builder import ContextBuilder
from .database import get_db_path, init_db
from .embedding_service import EmbeddingService

__all__ = [
    "init_db",
    "get_db_path",
    "EmbeddingService",
    "ContextBuilder",
]
