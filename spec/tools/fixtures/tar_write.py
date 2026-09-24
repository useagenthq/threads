# pyright: strict
"""Tar archives for the tree vectors, written with stdlib tarfile (spec/schema/README.md,
"Snapshot manifest"). Every header is deterministic: mtime 0, uid/gid 0, no owner names. An
archive ends right after its two zero blocks unless `pad` keeps tarfile's record padding."""

from __future__ import annotations

import base64
import io
import tarfile
from dataclasses import dataclass
from typing import Literal

BLOCK = 512

type Kind = Literal["file", "dir", "symlink", "hardlink", "chr", "fifo"]


@dataclass(frozen=True, slots=True)
class Member:
    """One archive entry: `data` for a file, `link` for a symlink or hardlink."""

    kind: Kind
    name: str
    mode: int = 0o644
    data: bytes = b""
    link: str = ""
    pax: tuple[tuple[str, str], ...] = ()


def file(name: str, data: bytes, mode: int = 0o644) -> Member:
    return Member("file", name, mode, data)


def folder(name: str, mode: int = 0o755) -> Member:
    return Member("dir", name, mode)


def symlink(name: str, target: str) -> Member:
    return Member("symlink", name, 0o777, link=target)


def hardlink(name: str, target: str) -> Member:
    return Member("hardlink", name, link=target)


_TYPES: dict[Kind, bytes] = {
    "file": tarfile.REGTYPE,
    "dir": tarfile.DIRTYPE,
    "symlink": tarfile.SYMTYPE,
    "hardlink": tarfile.LNKTYPE,
    "chr": tarfile.CHRTYPE,
    "fifo": tarfile.FIFOTYPE,
}


def _info(m: Member) -> tarfile.TarInfo:
    info = tarfile.TarInfo(m.name)
    info.type = _TYPES[m.kind]
    info.mode = m.mode
    info.size = len(m.data)
    info.linkname = m.link
    info.mtime = 0
    info.uname = info.gname = ""
    info.pax_headers = dict(m.pax)
    return info


def archive(members: tuple[Member, ...], fmt: int = tarfile.PAX_FORMAT, pad: bool = False) -> bytes:
    """The members as one archive, in order. Names that aren't UTF-8 are written from
    surrogate escapes."""
    out = io.BytesIO()
    with tarfile.open(
        fileobj=out, mode="w", format=fmt, encoding="utf-8", errors="surrogateescape"
    ) as tar:
        for m in members:
            tar.addfile(_info(m), io.BytesIO(m.data) if m.kind == "file" else None)
        end = tar.offset + 2 * BLOCK
    data = out.getvalue()
    return data if pad else data[:end]


def rechecksum(data: bytes, at: int) -> bytes:
    """Recomputes the checksum of the header block at byte `at`, after a deliberate edit."""
    block = bytearray(data[at : at + BLOCK])
    block[148:156] = b" " * 8
    block[148:156] = b"%06o\0 " % sum(block)
    return data[:at] + bytes(block) + data[at + BLOCK :]


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
