"""
Encryption utilities for multi-tenant data protection.

Implements a key hierarchy:
- Master Key (env var, rotatable)
  └── Per-User DEK (derived from user_id + master)
        └── Encrypts: API keys, sensitive preferences

All user-specific sensitive data is encrypted with a key derived from
their user_id, so even database access doesn't expose other users' data.
"""

import os
import base64
import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

# Master key from environment (required for encryption operations)
_master_key: Optional[str] = None


def _get_master_key() -> str:
    """Get the master encryption key from environment."""
    global _master_key
    if _master_key is None:
        _master_key = os.environ.get('LEGATO_MASTER_KEY')
        if not _master_key:
            # Generate a warning but allow operation in dev mode
            logger.warning(
                "LEGATO_MASTER_KEY not set. Encryption will use a default key. "
                "This is INSECURE and should only be used in development."
            )
            _master_key = "dev-only-insecure-key-do-not-use-in-production"
    return _master_key


def derive_user_key(user_id: str) -> bytes:
    """Derive a per-user encryption key from the master key.

    Uses PBKDF2 with the user_id as salt to create a unique key
    for each user. This means:
    - Each user's data is encrypted with a different key
    - Compromising one user's data doesn't expose others
    - Master key rotation requires re-encrypting all data

    Args:
        user_id: The user's unique identifier

    Returns:
        A 32-byte key suitable for Fernet encryption
    """
    master = _get_master_key()
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=user_id.encode('utf-8'),
        iterations=100_000,
    )
    derived = kdf.derive(master.encode('utf-8'))
    return base64.urlsafe_b64encode(derived)


def encrypt_for_user(user_id: str, plaintext: str) -> bytes:
    """Encrypt data for a specific user.

    Args:
        user_id: The user's unique identifier
        plaintext: The data to encrypt

    Returns:
        Encrypted bytes (Fernet token)
    """
    key = derive_user_key(user_id)
    f = Fernet(key)
    return f.encrypt(plaintext.encode('utf-8'))


def decrypt_for_user(user_id: str, ciphertext: bytes) -> Optional[str]:
    """Decrypt user's data.

    Args:
        user_id: The user's unique identifier
        ciphertext: The encrypted data (Fernet token)

    Returns:
        Decrypted string, or None if decryption fails
    """
    try:
        key = derive_user_key(user_id)
        f = Fernet(key)
        return f.decrypt(ciphertext).decode('utf-8')
    except InvalidToken:
        logger.error(f"Failed to decrypt data for user {user_id}: invalid token")
        return None
    except Exception as e:
        logger.error(f"Failed to decrypt data for user {user_id}: {e}")
        return None


def encrypt_api_key(user_id: str, api_key: str) -> tuple[bytes, str]:
    """Encrypt an API key and return the ciphertext and hint.

    Args:
        user_id: The user's unique identifier
        api_key: The API key to encrypt

    Returns:
        Tuple of (encrypted_key, key_hint)
        The hint is the last 4 characters for UI display
    """
    encrypted = encrypt_for_user(user_id, api_key)
    hint = api_key[-4:] if len(api_key) >= 4 else "****"
    return encrypted, hint


def decrypt_api_key(user_id: str, encrypted_key: bytes) -> Optional[str]:
    """Decrypt an API key.

    Args:
        user_id: The user's unique identifier
        encrypted_key: The encrypted API key

    Returns:
        The decrypted API key, or None if decryption fails
    """
    return decrypt_for_user(user_id, encrypted_key)


def generate_master_key() -> str:
    """Generate a new master key for initial setup.

    Returns:
        A URL-safe base64-encoded 32-byte key
    """
    return base64.urlsafe_b64encode(os.urandom(32)).decode('utf-8')


# Convenience function for generating keys during setup
if __name__ == '__main__':
    print("Generated master key (add to LEGATO_MASTER_KEY env var):")
    print(generate_master_key())
