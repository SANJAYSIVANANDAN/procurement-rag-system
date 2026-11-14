import os
from pathlib import Path
from typing import BinaryIO, Protocol
from uuid import UUID, uuid4


class FileStorage(Protocol):
    """Where uploaded files live. V1 is a local volume; S3 and others come later (spec 18.3)."""

    def temp_path(self) -> Path:
        """A fresh path on the same volume as final storage, so `put` can move atomically."""
        ...

    def put(
        self, owner_id: UUID, document_id: UUID, sha256: str, extension: str, source: Path
    ) -> str:
        """Move `source` into place and return its storage path (relative to the root)."""
        ...

    def open(self, path: str) -> BinaryIO: ...

    def local_path(self, path: str) -> Path:
        """A readable local file for this object (remote backends download it first)."""
        ...

    def delete(self, path: str) -> None: ...

    def exists(self, path: str) -> bool: ...


class LocalFileStorage:
    """Files at `{root}/{owner_id}/{document_id}/{sha256}.{ext}`, written with an atomic replace.

    Every path is generated from ids and a hash, never from user input, and every lookup is
    checked to stay inside the root.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._temp_dir = self._root / ".tmp"

    def temp_path(self) -> Path:
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        return self._temp_dir / uuid4().hex

    def put(
        self, owner_id: UUID, document_id: UUID, sha256: str, extension: str, source: Path
    ) -> str:
        relative = f"{owner_id}/{document_id}/{sha256}.{extension}"
        destination = self._resolve(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)
        return relative

    def open(self, path: str) -> BinaryIO:
        return self._resolve(path).open("rb")

    def local_path(self, path: str) -> Path:
        return self._resolve(path)

    def delete(self, path: str) -> None:
        target = self._resolve(path)
        target.unlink(missing_ok=True)
        try:
            target.parent.rmdir()  # the per-document directory, once it is empty
        except OSError:
            pass

    def exists(self, path: str) -> bool:
        return self._resolve(path).is_file()

    def _resolve(self, relative: str) -> Path:
        resolved = (self._root / relative).resolve()
        if not resolved.is_relative_to(self._root.resolve()):
            raise ValueError("storage path escapes the storage root")
        return resolved
