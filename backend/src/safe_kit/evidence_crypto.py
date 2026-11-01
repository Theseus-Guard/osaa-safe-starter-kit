"""Envelope encryption for evidence objects.

Each object gets a fresh 32-byte data key. The content is encrypted with AES-256-GCM under that key and a fresh 12-byte
nonce. The data key is wrapped by the key provider's key-encryption key. The stored record holds the ciphertext
(in the storage volume), the content nonce, the wrapped data key, and the key id (in the database).

The object's storage key is bound as associated data for both the content and the wrap. An encrypted object
therefore cannot be moved to another storage key or paired with another object's wrapped key without failing
authentication.

SHA-256 checksums are stored in the database, not in the storage. They detect accidental corruption and show that
a stored object is the one that was written. They do not protect against someone who can change both the object
and the database row.
"""

import hashlib
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from safe_kit.evidence_keys import KeyProvider, KeyProviderError

DATA_KEY_BYTES = 32
CONTENT_NONCE_BYTES = 12


class IntegrityError(Exception):
    """An object does not match its record. reason is a short code that is stored in the custody log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SealedObject:
    ciphertext: bytes
    nonce: bytes
    wrapped_data_key: bytes
    key_id: str


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _content_aad(storage_key: str) -> bytes:
    return b"safe-evidence-object:" + storage_key.encode("ascii")


def _wrap_aad(storage_key: str) -> bytes:
    return b"safe-evidence-dek:" + storage_key.encode("ascii")


def seal(plaintext: bytes, storage_key: str, provider: KeyProvider) -> SealedObject:
    """Encrypt one object with a fresh data key. The plain data key is not returned and is not kept."""
    data_key = secrets.token_bytes(DATA_KEY_BYTES)
    nonce = secrets.token_bytes(CONTENT_NONCE_BYTES)
    ciphertext = AESGCM(data_key).encrypt(nonce, plaintext, _content_aad(storage_key))
    wrapped = provider.wrap(data_key, _wrap_aad(storage_key))
    return SealedObject(ciphertext=ciphertext, nonce=nonce, wrapped_data_key=wrapped, key_id=provider.key_id)


def open_sealed(
    ciphertext: bytes,
    nonce: bytes,
    wrapped_data_key: bytes,
    key_id: str,
    storage_key: str,
    provider: KeyProvider,
) -> bytes:
    """Decrypt one object. Raises KeyProviderError for key problems and IntegrityError when authentication fails."""
    if key_id != provider.key_id:
        raise KeyProviderError(f"object was sealed with key id {key_id!r}, which is not the configured key")
    data_key = provider.unwrap(wrapped_data_key, _wrap_aad(storage_key))
    try:
        return AESGCM(data_key).decrypt(nonce, ciphertext, _content_aad(storage_key))
    except InvalidTag:
        raise IntegrityError("decryption_failed") from None
