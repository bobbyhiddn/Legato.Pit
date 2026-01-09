"""
Context Builder

Builds augmented prompts for RAG-enabled chat.
Retrieves relevant context from the knowledge base and formats it for the LLM.
"""

import logging
from typing import List, Dict, Optional

from .embedding_service import EmbeddingService

logger = logging.getLogger(__name__)


class ContextBuilder:
    """Builds context-augmented prompts for chat."""

    DEFAULT_MAX_ENTRIES = 10
    DEFAULT_THRESHOLD = 0.4
    DEFAULT_MAX_CONTEXT_LENGTH = 8000  # Characters

    def __init__(
        self,
        embedding_service: EmbeddingService,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        threshold: float = DEFAULT_THRESHOLD,
        max_context_length: int = DEFAULT_MAX_CONTEXT_LENGTH,
    ):
        """Initialize the context builder.

        Args:
            embedding_service: The embedding service for similarity search
            max_entries: Maximum context entries to include
            threshold: Minimum similarity threshold
            max_context_length: Maximum total context length in characters
        """
        self.embedding_service = embedding_service
        self.max_entries = max_entries
        self.threshold = threshold
        self.max_context_length = max_context_length

    def retrieve_context(self, query: str) -> List[Dict]:
        """Retrieve relevant context for a query.

        Args:
            query: The user's query

        Returns:
            List of relevant entries with metadata
        """
        return self.embedding_service.find_similar(
            query_text=query,
            limit=self.max_entries,
            threshold=self.threshold,
        )

    def format_context(self, entries: List[Dict]) -> str:
        """Format retrieved entries as context text.

        Args:
            entries: List of entries from retrieve_context

        Returns:
            Formatted context string
        """
        if not entries:
            return ""

        context_parts = []
        total_length = 0

        for entry in entries:
            # Format each entry
            entry_text = f"""--- Entry: {entry['title']} (relevance: {entry['similarity']:.2f}) ---
Category: {entry.get('category', 'N/A')}

{entry.get('content', '')}
--- End Entry ---"""

            # Check if adding this would exceed limit
            if total_length + len(entry_text) > self.max_context_length:
                # Truncate or skip
                remaining = self.max_context_length - total_length
                if remaining > 200:  # Only include if meaningful
                    entry_text = entry_text[:remaining] + "\n[truncated]"
                    context_parts.append(entry_text)
                break

            context_parts.append(entry_text)
            total_length += len(entry_text)

        return "\n\n".join(context_parts)

    def build_prompt(
        self,
        query: str,
        system_prompt: Optional[str] = None,
        include_context: bool = True,
    ) -> Dict[str, str]:
        """Build a complete prompt with retrieved context.

        Args:
            query: The user's query
            system_prompt: Optional custom system prompt
            include_context: Whether to include RAG context

        Returns:
            Dict with 'system', 'context', and 'user' keys
        """
        default_system = """You are a helpful AI assistant for the LEGATO knowledge management system.
You have access to a knowledge base of concepts, procedures, and references.
Use the provided context to answer questions accurately.
If the context doesn't contain relevant information, say so and provide general guidance.
Be concise and direct in your responses."""

        result = {
            'system': system_prompt or default_system,
            'context': '',
            'user': query,
            'context_entries': [],
        }

        if include_context:
            entries = self.retrieve_context(query)
            result['context'] = self.format_context(entries)
            result['context_entries'] = [
                {
                    'entry_id': e['entry_id'],
                    'title': e['title'],
                    'similarity': e['similarity'],
                }
                for e in entries
            ]

        return result

    def build_messages(
        self,
        query: str,
        history: Optional[List[Dict]] = None,
        system_prompt: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """Build a complete message list for chat APIs.

        Args:
            query: The user's current query
            history: Previous messages [{'role': 'user'|'assistant', 'content': str}]
            system_prompt: Optional custom system prompt

        Returns:
            List of message dicts for chat API
        """
        prompt_data = self.build_prompt(query, system_prompt)

        messages = [
            {'role': 'system', 'content': prompt_data['system']},
        ]

        # Add context as a system message if present
        if prompt_data['context']:
            messages.append({
                'role': 'system',
                'content': f"CONTEXT INFORMATION (from knowledge base):\n\n{prompt_data['context']}",
            })

        # Add conversation history
        if history:
            for msg in history:
                messages.append({
                    'role': msg['role'],
                    'content': msg['content'],
                })

        # Add current query
        messages.append({
            'role': 'user',
            'content': query,
        })

        return messages

    def get_stats(self) -> Dict:
        """Get statistics about the knowledge base.

        Returns:
            Dict with counts and info
        """
        conn = self.embedding_service.conn

        knowledge_count = conn.execute(
            "SELECT COUNT(*) FROM knowledge_entries"
        ).fetchone()[0]

        project_count = conn.execute(
            "SELECT COUNT(*) FROM project_entries"
        ).fetchone()[0]

        embedding_count = conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE vector_version = ?",
            (self.embedding_service.provider.model_identifier(),)
        ).fetchone()[0]

        return {
            'knowledge_entries': knowledge_count,
            'project_entries': project_count,
            'embeddings': embedding_count,
            'provider': self.embedding_service.provider.model_identifier(),
        }
