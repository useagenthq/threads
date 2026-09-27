"""Tar in and out of a container: the Engine API moves files only as archives.

Building: the supervisor's own archive, and every `SandboxSession.upload`. The uid and gid in
the headers are load-bearing — `docker cp` from a Mac would write the host's uid and the exec
would then fail with EACCES — so every entry names its owner.

Reading: what comes back is container output, so it is parsed defensively: regular files only,
a member and a total byte cap, and no path is ever written to the host's filesystem.
"""

import io
import tarfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

MEMBERS_MAX: Final = 4096
BYTES_MAX: Final = 64 * 1024 * 1024


class ArchiveError(Exception):
    """An archive the daemon returned that this adapter refuses to read."""


@dataclass(frozen=True, slots=True)
class Entry:
    """One archive member: a regular file, or a directory when `data` is None."""

    path: str
    mode: int
    uid: int
    gid: int
    data: bytes | None = None


def build_tar(entries: Sequence[Entry]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for entry in entries:
            info = tarfile.TarInfo(entry.path)
            info.mode, info.uid, info.gid, info.mtime = entry.mode, entry.uid, entry.gid, 0
            info.type = tarfile.REGTYPE if entry.data is not None else tarfile.DIRTYPE
            info.size = len(entry.data) if entry.data is not None else 0
            tar.addfile(info, io.BytesIO(entry.data) if entry.data is not None else None)
    return out.getvalue()


def read_tar(data: bytes) -> Mapping[str, bytes]:
    """The regular files of an archive, by their name as the archive gives it."""
    files: dict[str, bytes] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
            for info in tar:
                if len(files) >= MEMBERS_MAX:
                    raise ArchiveError(f"the archive holds over {MEMBERS_MAX} members")
                if not info.isreg():
                    continue
                total += info.size
                if total > BYTES_MAX:
                    raise ArchiveError(f"the archive holds over {BYTES_MAX} bytes")
                body = tar.extractfile(info)
                files[info.name] = b"" if body is None else body.read()
    except tarfile.TarError as broken:
        raise ArchiveError(f"the archive can't be read: {broken}") from broken
    return files
