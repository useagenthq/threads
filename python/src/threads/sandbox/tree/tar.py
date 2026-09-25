"""The streaming reader for untrusted tar archives (spec/schema/README.md, "Snapshot manifest").

It pulls exact byte counts from the source, so any chunking gives the same result; each regular
file streams into its own artifact as it arrives. Stdlib `tarfile` isn't used: in stream mode it
ends the archive silently at a bad header after the first entry, accepts formats and fields the
TypeScript reader refuses, and pulls from a blocking file object, while the source here is an
async stream. A small reader gives both languages the same rules.
"""

from collections.abc import AsyncIterable, Callable
from dataclasses import dataclass, replace
from functools import partial
from typing import Final, Literal, Protocol

from threads.result import Err, Ok
from threads.sandbox.tree.header import BLOCK, Header, Pax, is_zero, parse_header, parse_pax
from threads.sandbox.tree.paths import PathSet, decode_utf8, in_root, normalize
from threads.sandbox.tree.source import Source
from threads.sandbox.tree.tree import (
    Tree,
    TreeDir,
    TreeEntry,
    TreeFile,
    TreeSymlink,
    encode_tree,
    sorted_tree,
    tree_manifest_hash,
)
from threads.store.artifacts import ArtifactSink, ArtifactStore

type ArchiveReason = Literal[
    "bad_header",
    "truncated",
    "bad_path",
    "unsupported_type",
    "file_too_large",
    "archive_too_large",
    "bad_symlink",
    "bad_hardlink",
    "duplicate",
    "path_conflict",
]


@dataclass(frozen=True, slots=True)
class Caps:
    file: int = 256 * 2**20
    total: int = 2**30


CAPS: Final = Caps()
_META = 2**20
"""The most bytes of one pax or GNU long-name header's data."""


@dataclass(frozen=True, slots=True)
class ArchiveInvalid:
    """Why an archive was refused, naming its entry (None when no entry's name was read)."""

    reason: ArchiveReason
    entry: str | None
    message: str
    code: Literal["archive_invalid"] = "archive_invalid"


def _invalid(reason: ArchiveReason, entry: str | None, offset: int) -> Err[ArchiveInvalid]:
    where = f"at byte {offset}" if entry is None else f"entry {entry!r}"
    return Err(ArchiveInvalid(reason, entry, f"archive {where}: {reason.replace('_', ' ')}"))


def _padding(size: int) -> int:
    return -size % BLOCK


def _skip(_: bytes) -> None:
    pass


@dataclass(frozen=True, slots=True)
class _Pending:
    """The long names and pax records that apply to the next entry."""

    pax: Pax | None = None
    name: bytes | None = None
    link: bytes | None = None


class Offload(Protocol):
    """Runs one synchronous artifact call. `inline` for memory artifacts and hashing sinks; a
    store binds it to its worker thread (SqliteStore.put_tree), so a file sink's disk writes
    and fsyncs never block the event loop."""

    async def __call__[T](self, job: Callable[[], T], /) -> T: ...


async def inline[T](job: Callable[[], T], /) -> T:
    """Runs `job` right here: for sinks that never touch a disk."""
    return job()


@dataclass(frozen=True, slots=True)
class _State:
    src: Source
    caps: Caps
    open_sink: Callable[[], ArtifactSink]
    run: Offload
    paths: PathSet
    entries: dict[str, TreeEntry]
    """By path, in archive order."""


type _Step = Ok[_Pending] | Err[ArchiveInvalid]
type _Kind = Literal["file", "dir", "symlink", "hardlink"]
_KINDS: Final[dict[bytes, _Kind]] = {
    b"0": "file",
    b"\0": "file",
    b"5": "dir",
    b"2": "symlink",
    b"1": "hardlink",
}
_META_TYPES = frozenset((b"x", b"L", b"K"))


async def _meta(s: _State, h: Header, pending: _Pending, at: int) -> _Step:
    """A pax ('x'), GNU long name ('L') or long link ('K') header: its data names the next
    entry."""
    slot = {b"x": "pax", b"L": "name"}.get(h.type, "link")
    current = {"pax": pending.pax, "name": pending.name}.get(slot, pending.link)
    if current is not None or h.size > _META:
        return _invalid("bad_header", None, at)
    if s.src.offset + h.size + _padding(h.size) > s.caps.total:
        return _invalid("archive_too_large", None, at)
    data = await s.src.exact(h.size)
    if data is None or not await s.src.take(_padding(h.size), _skip):
        return _invalid("truncated", None, s.src.offset)
    if slot == "name":
        return Ok(replace(pending, name=data.split(b"\0", 1)[0]))
    if slot == "link":
        return Ok(replace(pending, link=data.split(b"\0", 1)[0]))
    pax = parse_pax(data)
    return _invalid("bad_header", None, at) if pax is None else Ok(replace(pending, pax=pax))


async def _file(s: _State, path: str, size: int, mode: int) -> Ok[TreeEntry] | Err[ArchiveInvalid]:
    """A file's bytes into a new artifact; its tree entry, or why the archive is refused."""
    sink = await s.run(s.open_sink)
    left = size
    while left:
        piece = await s.src.next(left)
        if piece is None:
            break
        await s.run(partial(sink.write, piece))
        left -= len(piece)
    if left or not await s.src.take(_padding(size), _skip):
        await s.run(sink.discard)
        return _invalid("truncated", path, s.src.offset)
    sha256 = await s.run(sink.commit)
    return Ok(TreeFile(path=path, kind="file", mode=mode, size=size, sha256=sha256))


def _linked(
    s: _State, kind: Literal["dir", "symlink", "hardlink"], path: str, h: Header, link: bytes
) -> TreeEntry | Literal["bad_symlink", "bad_hardlink"]:
    """The entry a link or directory header describes, or why it is refused."""
    if kind == "dir":
        return TreeDir(path=path, kind="dir", mode=h.mode)
    target = decode_utf8(link)
    if kind == "symlink":
        ok = target is not None and in_root(path, target)
        return TreeSymlink(path=path, kind="symlink", target=target or "") if ok else "bad_symlink"
    # Expanded: a regular file with its target's content and mode, as `find -type f` sees it.
    earlier = s.entries.get(normalize(target or "", is_dir=False) or "")
    if not isinstance(earlier, TreeFile):
        return "bad_hardlink"
    return TreeFile(
        path=path, kind="file", mode=earlier.mode, size=earlier.size, sha256=earlier.sha256
    )


@dataclass(frozen=True, slots=True)
class _Named:
    path: str
    kind: _Kind
    size: int


def _first(*options: bytes | None) -> bytes:
    """The first option present: a pax record, then a GNU long name, then the header's."""
    return next(o for o in options if o is not None)


def _named(h: Header, pending: _Pending, at: int) -> Ok[_Named] | Err[ArchiveInvalid]:
    """The entry's normalized path ("" for the root), kind and data size."""
    pax = pending.pax or Pax()
    raw = decode_utf8(_first(pax.path, pending.name, h.name))
    if raw is None:
        return _invalid("bad_path", None, at)
    kind = _KINDS.get(h.type)
    path = normalize(raw, is_dir=kind == "dir")
    if path is None or (path == "" and kind != "dir"):
        return _invalid("bad_path", raw, at)
    if kind is None:
        return _invalid("unsupported_type", path, at)
    size = h.size if pax.size is None else pax.size
    if kind != "file" and size != 0:
        return _invalid("bad_header", path, at)
    return Ok(_Named(path, kind, size))


def _admitted(
    s: _State, n: _Named, h: Header, pending: _Pending, at: int
) -> TreeEntry | Err[ArchiveInvalid] | None:
    """The caps, then the link rules, then the path's place in the tree. A regular file is
    None: its entry comes from its data."""
    if n.size > s.caps.file:
        return _invalid("file_too_large", n.path, at)
    if s.src.offset + n.size + _padding(n.size) > s.caps.total:
        return _invalid("archive_too_large", n.path, at)
    link = _first((pending.pax or Pax()).linkpath, pending.link, h.link)
    made = None if n.kind == "file" else _linked(s, n.kind, n.path, h, link)
    if isinstance(made, str):
        return _invalid(made, n.path, at)
    clash = s.paths.add(n.path, "file" if made is None else made.kind)
    return made if clash is None else _invalid(clash, n.path, at)


async def _entry(s: _State, h: Header, pending: _Pending, at: int) -> _Step:
    """One real entry: its checks in order, then its data."""
    match _named(h, pending, at):
        case Err() as failed:
            return failed
        case Ok(value=named):
            pass
    if named.path == "":
        return Ok(_Pending())
    made = _admitted(s, named, h, pending, at)
    if isinstance(made, Err):
        return made
    stored = await _file(s, named.path, named.size, h.mode) if made is None else Ok(made)
    if isinstance(stored, Ok):
        s.entries[named.path] = stored.value
    return Ok(_Pending()) if isinstance(stored, Ok) else stored


async def _end(s: _State, pending: _Pending) -> Ok[Tree] | Err[ArchiveInvalid]:
    """After the first zero block: a second one, then only zeros to the end of the source."""
    at = s.src.offset
    if pending != _Pending():
        return _invalid("bad_header", None, at)
    if at + BLOCK > s.caps.total:
        return _invalid("archive_too_large", None, at)
    second = await s.src.exact(BLOCK)
    if second is None:
        return _invalid("truncated", None, s.src.offset)
    if not is_zero(second):
        return _invalid("bad_header", None, at)
    stray = await _trailer(s)
    return Ok(sorted_tree(tuple(s.entries.values()))) if stray is None else stray


async def _trailer(s: _State) -> Err[ArchiveInvalid] | None:
    """Only zero bytes may follow the end blocks, and only up to the total cap."""
    while True:
        start = s.src.offset
        piece = await s.src.next(BLOCK * 64)
        if piece is None:
            return None
        inside = piece[: max(0, s.caps.total - start)]
        rest = inside.lstrip(b"\0")
        if rest:
            return _invalid("bad_header", None, start + len(inside) - len(rest))
        if len(inside) < len(piece):
            return _invalid("archive_too_large", None, s.caps.total)


async def read_tar(
    source: AsyncIterable[bytes],
    open_sink: Callable[[], ArtifactSink],
    caps: Caps = CAPS,
    run: Offload = inline,
) -> Ok[Tree] | Err[ArchiveInvalid]:
    """Reads an untrusted tar stream into a tree, each regular file streamed into an artifact
    from `open_sink`, every sink call made through `run`. A refused archive may leave the files
    it already stored, unreferenced."""
    s = _State(Source(source), caps, open_sink, run, PathSet(), {})
    pending = _Pending()
    try:
        while True:
            at = s.src.offset
            if at + BLOCK > caps.total:
                return _invalid("archive_too_large", None, at)
            block = await s.src.exact(BLOCK)
            if block is None:
                return _invalid("truncated", None, s.src.offset)
            if is_zero(block):
                return await _end(s, pending)
            h = parse_header(block)
            if h is None:
                return _invalid("bad_header", None, at)
            step = await (_meta if h.type in _META_TYPES else _entry)(s, h, pending, at)
            if isinstance(step, Err):
                return step
            pending = step.value
    finally:
        await s.src.close()


@dataclass(frozen=True, slots=True)
class StoredTree:
    """A stored tree: its artifact, and the manifest hash of its files."""

    tree: Tree
    sha256: str
    manifest_hash: str


async def store_tar(
    source: AsyncIterable[bytes], artifacts: ArtifactStore, caps: Caps = CAPS, run: Offload = inline
) -> Ok[StoredTree] | Err[ArchiveInvalid]:
    """Reads the archive into `artifacts`: the files first, then the tree artifact that lists
    them. Tree bytes are stored as they are: never redacted, which would change their hashes."""
    match await read_tar(source, artifacts.sink, caps, run):
        case Err() as failed:
            return failed
        case Ok(value=tree):
            sha256 = await run(partial(artifacts.put, encode_tree(tree)))
            return Ok(StoredTree(tree, sha256, tree_manifest_hash(tree)))
