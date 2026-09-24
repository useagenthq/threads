"""Content-addressed artifacts: `<root>/sha256/<ab>/<64hex>`.

An artifact is durable before any row or event references it, and its hash is verified on
every read. A missing or changed artifact is a typed error, never a substitute. Methods are
synchronous: the store calls them on its worker thread.

Large outputs are written through a sink, a chunk at a time, so they are never held whole in
memory: the hash is computed as the bytes arrive.
"""

import hashlib
import os
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from threads.log import ParseError
from threads.log.digest import sha256_hex
from threads.result import Err, Ok
from threads.store.worker import StoreError


class ArtifactSink(Protocol):
    def write(self, chunk: bytes) -> None: ...

    def commit(self) -> str:
        """Makes the bytes a durable artifact and returns their sha256."""
        ...

    def discard(self) -> None: ...


class MemoryArtifacts:
    """Artifacts for an in-memory store: the same contract, no files."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put(self, data: bytes) -> str:
        sha = sha256_hex(data)
        self._blobs[sha] = data
        return sha

    def get(self, sha256: str) -> Ok[bytes] | Err[ParseError]:
        data = self._blobs.get(sha256)
        return _missing(sha256) if data is None else Ok(data)

    def sink(self) -> ArtifactSink:
        return _MemorySink(self)


class _MemorySink:
    def __init__(self, store: MemoryArtifacts) -> None:
        self._store = store
        self._data = bytearray()

    def write(self, chunk: bytes) -> None:
        self._data += chunk

    def commit(self) -> str:
        return self._store.put(bytes(self._data))

    def discard(self) -> None:
        self._data.clear()


class FileArtifacts:
    """Artifacts on disk, shared by every thread of one home. Directories 0700, files 0600."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def put(self, data: bytes) -> str:
        sink = self.sink()
        sink.write(data)
        return sink.commit()

    def get(self, sha256: str) -> Ok[bytes] | Err[ParseError]:
        try:
            data = self.path(sha256).read_bytes()
        except FileNotFoundError:
            return _missing(sha256)
        except OSError as error:
            raise StoreError(str(error)) from error
        if sha256_hex(data) != sha256:
            return Err(ParseError("artifact_corrupt", f"artifact {sha256} fails its hash"))
        return Ok(data)

    def sink(self) -> ArtifactSink:
        with _disk():
            # mkdir(parents=True) would give the parents the umask's mode, not 0700.
            self._root.mkdir(mode=0o700, exist_ok=True)
            return _FileSink(self, self._root)

    def path(self, sha256: str) -> Path:
        return self._root / "sha256" / sha256[:2] / sha256


class _FileSink:
    """A temp file beside the tree, fsynced and hard-linked into place on commit (EEXIST:
    verify the hash, done), then the directory is fsynced."""

    def __init__(self, store: FileArtifacts, root: Path) -> None:
        self._store = store
        fd, name = tempfile.mkstemp(dir=root)  # created 0600
        self._file = os.fdopen(fd, "wb")
        self._temp = Path(name)
        self._hash = hashlib.sha256()

    def write(self, chunk: bytes) -> None:
        with _disk():
            self._file.write(chunk)
        self._hash.update(chunk)

    def commit(self) -> str:
        with _disk():
            return self._commit()

    def _commit(self) -> str:
        sha = self._hash.hexdigest()
        path = self._store.path(sha)
        try:
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()
            for directory in (path.parents[1], path.parent):
                directory.mkdir(mode=0o700, exist_ok=True)
            try:
                os.link(self._temp, path)
            except FileExistsError:
                if isinstance(self._store.get(sha), Err):
                    raise
        finally:
            self.discard()
        _fsync_dir(path.parent)
        return sha

    def discard(self) -> None:
        self._file.close()
        self._temp.unlink(missing_ok=True)


type ArtifactStore = MemoryArtifacts | FileArtifacts


@contextmanager
def _disk() -> Generator[None]:
    """The file system's errors under the artifacts, raised as the store's outage."""
    try:
        yield
    except OSError as error:
        raise StoreError(str(error)) from error


def _missing(sha256: str) -> Err[ParseError]:
    return Err(ParseError("artifact_missing", f"no artifact {sha256}"))


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
