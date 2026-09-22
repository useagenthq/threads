"""Content-addressed artifacts: `<root>/sha256/<ab>/<64hex>`.

An artifact is durable before any row or event references it, and its hash is verified on
every read. A missing or changed artifact is a typed error, never a substitute. Methods are
synchronous: the store calls them on its worker thread.
"""

import os
import tempfile
from pathlib import Path

from threads.log import ParseError
from threads.log.digest import sha256_hex
from threads.result import Err, Ok


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


class FileArtifacts:
    """Artifacts on disk, shared by every thread of one home. Directories 0700, files 0600."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def put(self, data: bytes) -> str:
        sha = sha256_hex(data)
        path = self._path(sha)
        # mkdir(parents=True) would give the parents the umask's mode, not 0700.
        for directory in (self._root, self._root / "sha256", path.parent):
            directory.mkdir(mode=0o700, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=path.parent)  # created 0600
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            try:
                os.link(temp, path)
            except FileExistsError:
                if isinstance(self.get(sha), Err):
                    raise
        finally:
            os.unlink(temp)
        _fsync_dir(path.parent)
        return sha

    def get(self, sha256: str) -> Ok[bytes] | Err[ParseError]:
        try:
            data = self._path(sha256).read_bytes()
        except FileNotFoundError:
            return _missing(sha256)
        if sha256_hex(data) != sha256:
            return Err(ParseError("artifact_corrupt", f"artifact {sha256} fails its hash"))
        return Ok(data)

    def _path(self, sha256: str) -> Path:
        return self._root / "sha256" / sha256[:2] / sha256


type ArtifactStore = MemoryArtifacts | FileArtifacts


def _missing(sha256: str) -> Err[ParseError]:
    return Err(ParseError("artifact_missing", f"no artifact {sha256}"))


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
