from pathlib import Path
from uuid import uuid4

import pytest

from covenantiq.ingestion.storage import FileStorage, LocalFileStorage


@pytest.fixture
def storage(tmp_path: Path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "uploads")


def stage(storage: LocalFileStorage, content: bytes = b"hello") -> Path:
    path = storage.temp_path()
    path.write_bytes(content)
    return path


def test_satisfies_the_storage_protocol(storage: LocalFileStorage) -> None:
    protocol_user: FileStorage = storage
    assert protocol_user is storage


def test_put_places_the_file_under_owner_and_document(storage: LocalFileStorage) -> None:
    owner, document = uuid4(), uuid4()
    source = stage(storage, b"contract bytes")
    path = storage.put(owner, document, "ab" * 32, "pdf", source)

    assert path == f"{owner}/{document}/{'ab' * 32}.pdf"
    assert not source.exists()  # moved, not copied
    with storage.open(path) as handle:
        assert handle.read() == b"contract bytes"
    assert storage.exists(path)


def test_temp_files_live_on_the_same_volume_as_final_storage(
    storage: LocalFileStorage, tmp_path: Path
) -> None:
    temp = storage.temp_path()
    assert temp.parent == tmp_path / "uploads" / ".tmp"
    assert storage.temp_path() != temp


def test_directories_are_created_lazily(tmp_path: Path) -> None:
    root = tmp_path / "never-used"
    LocalFileStorage(root)
    assert not root.exists()


def test_put_replaces_an_existing_file_atomically(storage: LocalFileStorage) -> None:
    owner, document = uuid4(), uuid4()
    storage.put(owner, document, "c" * 64, "pdf", stage(storage, b"old"))
    path = storage.put(owner, document, "c" * 64, "pdf", stage(storage, b"new"))
    with storage.open(path) as handle:
        assert handle.read() == b"new"


def test_delete_removes_the_file_and_its_empty_directory(
    storage: LocalFileStorage, tmp_path: Path
) -> None:
    owner, document = uuid4(), uuid4()
    path = storage.put(owner, document, "d" * 64, "docx", stage(storage))
    storage.delete(path)
    assert not storage.exists(path)
    assert not (tmp_path / "uploads" / str(owner) / str(document)).exists()


def test_delete_of_a_missing_file_is_a_no_op(storage: LocalFileStorage) -> None:
    storage.delete(f"{uuid4()}/{uuid4()}/missing.pdf")


@pytest.mark.parametrize(
    "path", ["../outside.pdf", "../../etc/passwd", "a/../../outside", "/etc/passwd"]
)
def test_paths_cannot_escape_the_root(storage: LocalFileStorage, path: str) -> None:
    for operation in (storage.open, storage.delete, storage.exists):
        with pytest.raises(ValueError, match="escapes"):
            operation(path)


def test_different_owners_never_share_a_directory(storage: LocalFileStorage) -> None:
    document = uuid4()
    a = storage.put(uuid4(), document, "e" * 64, "pdf", stage(storage, b"a"))
    b = storage.put(uuid4(), document, "e" * 64, "pdf", stage(storage, b"b"))
    assert a.split("/")[0] != b.split("/")[0]
