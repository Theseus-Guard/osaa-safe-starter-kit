"""Key providers for evidence envelope encryption.

A key provider holds a key-encryption key (KEK). It wraps the fresh data key of each evidence object and unwraps it
on read. The KEK never leaves the provider, is never written to the database, and is never logged.

LocalFileKeyProvider reads the KEK from the file named by SAFE_EVIDENCE_KEK_FILE.
DEVELOPMENT ONLY. The key sits on the same disk and in the same container as the data it protects, so it offers no
protection against anyone who can read both. The file must hold 32 random bytes, either raw or as base64 text, and
must not be accessible by group or others (mode 600). Generate one with: head -c 32 /dev/urandom > FILE && chmod 600 FILE
A file that is missing, empty, malformed, or has group or world permissions is refused.

VaultTransitKeyProvider is the planned production provider (HashiCorp Vault Transit). It is a stub and raises
NotImplementedError. Nothing calls Vault yet.
"""

import base64
import binascii
import os
import re
import stat
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEK_BYTES = 32
WRAP_NONCE_BYTES = 12
DEFAULT_KEY_ID = "local-file-1"
KEY_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")
PROVIDER_LOCAL_FILE = "local-file"
PROVIDER_VAULT_TRANSIT = "vault-transit"


class KeyProviderError(Exception):
    """The key provider is not configured, or it cannot unwrap a data key. The message never contains key material."""


class KeyProvider(Protocol):
    key_id: str

    def wrap(self, data_key: bytes, context: bytes) -> bytes:
        """Encrypt a data key. context is bound to the wrap as associated data."""
        ...

    def unwrap(self, wrapped: bytes, context: bytes) -> bytes:
        """Decrypt a wrapped data key. Raises KeyProviderError when the key or the context does not match."""
        ...


def _read_kek_file(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        raise KeyProviderError("KEK file is missing") from None
    except OSError:
        raise KeyProviderError("KEK file cannot be opened") from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise KeyProviderError("KEK path is not a regular file")
        if info.st_mode & 0o077:
            raise KeyProviderError("KEK file is accessible by group or others: set its mode to 600")
        raw = handle.read()
    if not raw.strip():
        raise KeyProviderError("KEK file is empty")
    if len(raw) == KEK_BYTES:
        return raw
    try:
        decoded = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise KeyProviderError("KEK file must hold 32 raw bytes or base64 text") from None
    if len(decoded) != KEK_BYTES:
        raise KeyProviderError("KEK file must hold exactly 32 bytes of key material")
    return decoded


class LocalFileKeyProvider:
    """Development-only KEK loaded from a file. Refuses to start (constructor raises) on a bad file.

    See the module docstring: this is not a production key store.
    """

    def __init__(self, path: str | os.PathLike, key_id: str = DEFAULT_KEY_ID) -> None:
        if not KEY_ID_PATTERN.fullmatch(key_id):
            raise KeyProviderError("key id may contain only letters, digits, dots, underscores, and hyphens")
        self.key_id = key_id
        self._kek = _read_kek_file(Path(path))

    def wrap(self, data_key: bytes, context: bytes) -> bytes:
        nonce = os.urandom(WRAP_NONCE_BYTES)
        return nonce + AESGCM(self._kek).encrypt(nonce, data_key, context)

    def unwrap(self, wrapped: bytes, context: bytes) -> bytes:
        if len(wrapped) <= WRAP_NONCE_BYTES:
            raise KeyProviderError("wrapped data key is malformed")
        nonce, body = wrapped[:WRAP_NONCE_BYTES], wrapped[WRAP_NONCE_BYTES:]
        try:
            return AESGCM(self._kek).decrypt(nonce, body, context)
        except InvalidTag:
            raise KeyProviderError("data key cannot be unwrapped: wrong key or altered record") from None

    def __repr__(self) -> str:
        return f"LocalFileKeyProvider(key_id={self.key_id!r})"


_VAULT_NOT_IMPLEMENTED = (
    "VaultTransitKeyProvider is not implemented yet. "
    "Use SAFE_EVIDENCE_KEY_PROVIDER=local-file (development only) until the Vault provider is built."
)


class VaultTransitKeyProvider:
    """Planned production provider backed by HashiCorp Vault Transit. Stub: every use raises NotImplementedError."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError(_VAULT_NOT_IMPLEMENTED)

    def wrap(self, data_key: bytes, context: bytes) -> bytes:
        raise NotImplementedError(_VAULT_NOT_IMPLEMENTED)

    def unwrap(self, wrapped: bytes, context: bytes) -> bytes:
        raise NotImplementedError(_VAULT_NOT_IMPLEMENTED)


def key_provider_from_env() -> KeyProvider:
    """Build the provider named by SAFE_EVIDENCE_KEY_PROVIDER (default local-file).

    For local-file: SAFE_EVIDENCE_KEK_FILE is required and SAFE_EVIDENCE_KEK_ID names the key (default local-file-1).
    """
    name = os.environ.get("SAFE_EVIDENCE_KEY_PROVIDER", PROVIDER_LOCAL_FILE)
    if name == PROVIDER_LOCAL_FILE:
        path = os.environ.get("SAFE_EVIDENCE_KEK_FILE", "")
        if not path:
            raise KeyProviderError("SAFE_EVIDENCE_KEK_FILE is not set")
        return LocalFileKeyProvider(path, os.environ.get("SAFE_EVIDENCE_KEK_ID", DEFAULT_KEY_ID))
    if name == PROVIDER_VAULT_TRANSIT:
        return VaultTransitKeyProvider()
    raise KeyProviderError("SAFE_EVIDENCE_KEY_PROVIDER must be local-file or vault-transit")
