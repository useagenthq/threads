"""The canonical archive of a tree (spec/schema/README.md, "Snapshot manifest"): ustar headers
in tree order, a pax header only for a name or link over 100 bytes, mtime 0, the caller's
uid/gid. A tree's paths are normal and its symlinks stay in the root, so extracting the archive
can't write outside it."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import assert_never

from threads.log import ParseError
from threads.render.artifacts import ReadArtifact
from threads.result import Err, Ok
from threads.sandbox.tree.header import BLOCK
from threads.sandbox.tree.tree import Tree, TreeDir, TreeEntry, TreeFile, TreeSymlink, broken

_FIELD = 100
_MAX_ID = 0o7777777


@dataclass(frozen=True, slots=True)
class Owner:
    """The owner every entry is written with (1000 for Docker's command user)."""

    uid: int
    gid: int


def _octal(value: int, digits: int) -> bytes:
    if value >= 8**digits:
        # A tree's files are under the 256 MiB cap, so a size never overflows its field.
        raise ValueError(f"{value} doesn't fit {digits} octal digits")
    return b"%0*o\0" % (digits, value)


@dataclass(frozen=True, slots=True)
class _Fields:
    kind: bytes
    name: bytes
    link: bytes
    mode: int
    size: int


def _header(f: _Fields, owner: Owner) -> bytes:
    block = bytearray(BLOCK)
    block[0:_FIELD] = f.name[:_FIELD].ljust(_FIELD, b"\0")
    block[100:108] = _octal(f.mode, 7)
    block[108:116] = _octal(owner.uid, 7)
    block[116:124] = _octal(owner.gid, 7)
    block[124:136] = _octal(f.size, 11)
    block[136:148] = _octal(0, 11)
    block[156:157] = f.kind
    block[157 : 157 + _FIELD] = f.link[:_FIELD].ljust(_FIELD, b"\0")
    block[257:265] = b"ustar\x0000"
    block[329:337] = _octal(0, 7)
    block[337:345] = _octal(0, 7)
    block[148:156] = b" " * 8
    block[148:156] = b"%06o\0 " % sum(block)
    return bytes(block)


def _pax_record(key: bytes, value: bytes) -> bytes:
    """One pax record; its length counts its own digits."""
    body = 3 + len(key) + len(value)
    length = body + len(str(body))
    if len(str(length)) > len(str(body)):
        length += 1
    return b"%d %s=%s\n" % (length, key, value)


def _padding(size: int) -> bytes:
    return bytes(-size % BLOCK)


def _headers(e: TreeEntry, owner: Owner) -> list[bytes]:
    """The headers of one entry: a pax header first when its name or link is too long."""
    match e:
        case TreeFile():
            f = _Fields(b"0", e.path.encode(), b"", e.mode, e.size)
        case TreeDir():
            f = _Fields(b"5", f"{e.path}/".encode(), b"", e.mode, 0)
        case TreeSymlink():
            f = _Fields(b"2", e.path.encode(), e.target.encode(), 0o777, 0)
        case _:
            assert_never(e)
    records = (_pax_record(b"path", f.name) if len(f.name) > _FIELD else b"") + (
        _pax_record(b"linkpath", f.link) if len(f.link) > _FIELD else b""
    )
    own = _header(f, owner)
    if not records:
        return [own]
    pax = _header(_Fields(b"x", b"././@PaxHeader", b"", 0o644, len(records)), owner)
    return [pax, records, _padding(len(records)), own]


def build_tar(
    tree: Tree, read: ReadArtifact, owner: Owner, out: Callable[[bytes], None]
) -> Ok[None] | Err[ParseError]:
    """Writes the tree's canonical archive to `out`, one file's bytes at a time from
    `read`. A missing or corrupt file artifact, or one whose size isn't the tree's, stops
    it. A tree that breaks a tree rule is artifact_corrupt before any byte is written."""
    if not all(0 <= i <= _MAX_ID for i in (owner.uid, owner.gid)):
        raise ValueError(f"uid/gid outside 0..{_MAX_ID}")
    why = broken(tree.entries)
    if why is not None:
        return Err(ParseError("artifact_corrupt", f"the tree can't be built: {why}"))
    for e in tree.entries:
        for block in _headers(e, owner):
            out(block)
        if not isinstance(e, TreeFile):
            continue
        match read(e.sha256):
            case Err() as failed:
                return failed
            case Ok(value=data) if len(data) != e.size:
                message = f"artifact {e.sha256} isn't {e.size} bytes long"
                return Err(ParseError("artifact_corrupt", message))
            case Ok(value=data):
                out(data)
                out(_padding(e.size))
    out(bytes(2 * BLOCK))
    return Ok(None)
