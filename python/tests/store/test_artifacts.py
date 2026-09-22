"""The content-addressed artifact store: verified on every read."""

import hashlib
from pathlib import Path

from threads.result import Err, Ok
from threads.store.artifacts import FileArtifacts, MemoryArtifacts

DATA = b"dropped bytes"
SHA = hashlib.sha256(DATA).hexdigest()
FILE_MODE, DIR_MODE = 0o600, 0o700


def test_file_artifacts_round_trip_with_private_permissions(tmp_path: Path) -> None:
    store = FileArtifacts(tmp_path / "artifacts")
    assert store.put(DATA) == SHA
    assert store.put(DATA) == SHA
    path = tmp_path / "artifacts" / "sha256" / SHA[:2] / SHA
    assert path.stat().st_mode & 0o777 == FILE_MODE
    for directory in (path.parent, path.parent.parent, path.parent.parent.parent):
        assert directory.stat().st_mode & 0o777 == DIR_MODE
    assert store.get(SHA) == Ok(DATA)
    assert [p.name for p in path.parent.iterdir()] == [SHA]


def test_a_changed_artifact_is_corrupt(tmp_path: Path) -> None:
    store = FileArtifacts(tmp_path)
    store.put(DATA)
    (tmp_path / "sha256" / SHA[:2] / SHA).write_bytes(b"other bytes")
    got = store.get(SHA)
    assert isinstance(got, Err)
    assert got.error.code == "artifact_corrupt"


def test_an_absent_artifact_is_missing(tmp_path: Path) -> None:
    for store in (FileArtifacts(tmp_path), MemoryArtifacts()):
        got = store.get(SHA)
        assert isinstance(got, Err)
        assert got.error.code == "artifact_missing"
    memory = MemoryArtifacts()
    assert memory.get(memory.put(DATA)) == Ok(DATA)
