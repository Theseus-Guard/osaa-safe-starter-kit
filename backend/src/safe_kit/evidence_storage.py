"""Storage for encrypted evidence objects.

EvidenceStorage is the interface. It takes and returns whole objects as bytes, which is enough for the 50 MiB
default limit. An S3 or MinIO implementation can implement the same three methods later.

LocalVolumeStorage is the MVP implementation. It keeps one file per object under SAFE_EVIDENCE_DIR, which will be a
named Docker volume. Object keys are random UUIDs chosen by the application. They are never derived from a file name,
and anything that is not a canonical UUID is refused, so no path can escape the directory.
"""

import os
import re
import tempfile
from pathlib import Path
from typing import Protocol

DEFAULT_EVIDENCE_DIR = "/var/lib/safe/evidence"
OBJECT_KEY_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TEMP_PREFIX = ".tmp-"


class StorageError(Exception):
    """An object could not be read or written."""


class InvalidObjectKey(StorageError):
    """The object key is not a canonical UUID. Rejected before any file is touched."""


class ObjectNotFound(StorageError):
    """No object is stored under this key."""


class EvidenceStorage(Protocol):
    def put(self, object_key: str, data: bytes) -> None:
        """Store a new object. Writes are atomic: the object is either complete or absent."""
        ...

    def get(self, object_key: str) -> bytes:
        """Return the whole object. Raises ObjectNotFound when it is absent."""
        ...

    def exists(self, object_key: str) -> bool:
        ...


def _check_key(object_key: str) -> str:
    if not isinstance(object_key, str) or not OBJECT_KEY_PATTERN.fullmatch(object_key):
        raise InvalidObjectKey("object key must be a lowercase canonical UUID")
    return object_key


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class LocalVolumeStorage:
    def __init__(self, root: str | os.PathLike) -> None:
        self._root = Path(root)

    def _path(self, object_key: str) -> Path:
        path = self._root / _check_key(object_key)
        # The key pattern already excludes separators and dots. This check is a second guard against traversal.
        if path.parent != self._root:
            raise InvalidObjectKey("object key resolves outside the evidence directory")
        return path

    def put(self, object_key: str, data: bytes) -> None:
        final = self._path(object_key)
        if final.exists():
            raise StorageError("object already exists")
        try:
            fd, temp_name = tempfile.mkstemp(dir=self._root, prefix=TEMP_PREFIX, suffix=".part")
        except OSError as error:
            raise StorageError("evidence directory is not writable") from error
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, final)
        except OSError as error:
            temp.unlink(missing_ok=True)
            raise StorageError("object could not be written") from error
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        try:
            _fsync_directory(self._root)
        except OSError:
            # The rename is done. Only its durability across a crash is unconfirmed, so this is not an error here.
            pass

    def get(self, object_key: str) -> bytes:
        path = self._path(object_key)
        try:
            return path.read_bytes()
        except FileNotFoundError as error:
            raise ObjectNotFound("object is missing") from error
        except OSError as error:
            raise StorageError("object could not be read") from error

    def exists(self, object_key: str) -> bool:
        return self._path(object_key).is_file()


def storage_from_env() -> LocalVolumeStorage:
    return LocalVolumeStorage(os.environ.get("SAFE_EVIDENCE_DIR", DEFAULT_EVIDENCE_DIR))
