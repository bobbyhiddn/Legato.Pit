"""
OpenAI Whisper Transcription Service

Uses OpenAI's Whisper API to transcribe audio files.
Supports long recordings by chunking audio.
"""

import os
import io
import logging
import tempfile
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)


class WhisperService:
    """OpenAI Whisper transcription service."""

    TRANSCRIPTION_URL = "https://api.openai.com/v1/audio/transcriptions"
    DEFAULT_MODEL = "whisper-1"
    # Whisper API has a 25MB file size limit
    MAX_FILE_SIZE = 25 * 1024 * 1024  # 25MB

    def __init__(self, api_key: Optional[str] = None, model: str = DEFAULT_MODEL):
        """Initialize the Whisper service.

        Args:
            api_key: OpenAI API key (defaults to OPENAI_API_KEY env var)
            model: Model to use for transcription
        """
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OpenAI API key not provided and OPENAI_API_KEY not set")

        self.model = model
        logger.info(f"Whisper service initialized with model: {model}")

    def transcribe(
        self,
        audio_data: bytes,
        filename: str = "audio.webm",
        language: Optional[str] = None,
        prompt: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Transcribe audio data using Whisper API.

        Args:
            audio_data: Raw audio bytes
            filename: Original filename (helps API detect format)
            language: Optional language code (e.g., 'en')
            prompt: Optional prompt to guide transcription

        Returns:
            Tuple of (success: bool, transcript_or_error: str)
        """
        if not audio_data:
            return False, "No audio data provided"

        if len(audio_data) > self.MAX_FILE_SIZE:
            return False, f"Audio file too large (max {self.MAX_FILE_SIZE // (1024*1024)}MB)"

        # Determine file extension for Content-Type
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'webm'

        # Map extension to MIME type
        mime_types = {
            'webm': 'audio/webm',
            'mp3': 'audio/mpeg',
            'mp4': 'audio/mp4',
            'm4a': 'audio/mp4',
            'wav': 'audio/wav',
            'ogg': 'audio/ogg',
            'flac': 'audio/flac',
        }
        mime_type = mime_types.get(ext, 'audio/webm')

        try:
            # Prepare multipart form data
            files = {
                'file': (filename, audio_data, mime_type)
            }
            data = {
                'model': self.model,
                'response_format': 'text',  # Plain text response
            }

            if language:
                data['language'] = language
            if prompt:
                data['prompt'] = prompt

            logger.info(f"Sending audio to Whisper API: {len(audio_data)} bytes, format={ext}")

            response = requests.post(
                self.TRANSCRIPTION_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                },
                files=files,
                data=data,
                timeout=300,  # 5 minute timeout for long recordings
            )

            if response.status_code == 200:
                transcript = response.text.strip()
                logger.info(f"Transcription successful: {len(transcript)} chars")
                return True, transcript
            else:
                error_msg = f"Whisper API error: {response.status_code}"
                try:
                    error_data = response.json()
                    if 'error' in error_data:
                        error_msg = error_data['error'].get('message', error_msg)
                except:
                    error_msg = response.text[:200] if response.text else error_msg
                logger.error(error_msg)
                return False, error_msg

        except requests.exceptions.Timeout:
            logger.error("Whisper API request timed out")
            return False, "Transcription timed out. Try a shorter recording."
        except requests.exceptions.RequestException as e:
            logger.error(f"Whisper API request failed: {e}")
            return False, f"Network error: {str(e)}"
        except Exception as e:
            logger.error(f"Unexpected error during transcription: {e}")
            return False, f"Transcription error: {str(e)}"

    def is_available(self) -> bool:
        """Check if the Whisper service is available."""
        return bool(self.api_key)


# Singleton instance (lazy initialization)
_whisper_service: Optional[WhisperService] = None


def get_whisper_service() -> Optional[WhisperService]:
    """Get or create the Whisper service singleton.

    Returns:
        WhisperService instance or None if API key not configured
    """
    global _whisper_service
    if _whisper_service is None:
        try:
            _whisper_service = WhisperService()
        except ValueError:
            logger.warning("Whisper service not available: OPENAI_API_KEY not set")
            return None
    return _whisper_service
