"""Path rules shared by the archive reader and the tree parser (spec/schema/README.md,
"Snapshot manifest"). Paths are relative to the root and "/"-separated; a backslash is an ordinary
character."""

from typing import Literal

type Kind = Literal["file", "dir", "symlink"]
type Clash = Literal["duplicate", "path_conflict"]


def decode_utf8(data: bytes) -> str | None:
    """The bytes as UTF-8 text; None when they aren't UTF-8. Strict, so two different byte
    paths never decode to one name."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


MAX_PATH = 4096
"""The longest path, in UTF-8 bytes (Linux PATH_MAX): it keeps every path check bounded."""


def is_normal(path: str) -> bool:
    """At most MAX_PATH bytes, and every component a real name: no empty, `.` or `..`, no
    NUL."""
    return (
        # A UTF-8 byte is at least a quarter of a character; skip encoding a huge path.
        len(path) <= MAX_PATH
        and len(path.encode("utf-8", "surrogatepass")) <= MAX_PATH
        and "\0" not in path
        and all(c not in ("", ".", "..") for c in path.split("/"))
    )


def normalize(raw: str, *, is_dir: bool) -> str | None:
    """An archive name as the tree names it: one leading `./` stripped, then one trailing `/` of
    a directory. "" is the root. None: absolute, or not normal."""
    if raw.startswith("/"):
        return None
    path = raw.removeprefix("./")
    if is_dir:
        path = path.removesuffix("/")
    if path in ("", "."):
        return ""
    return path if is_normal(path) else None


def in_root(path: str, target: str) -> bool:
    """A symlink at `path` whose target stays in the root lexically, resolved from its
    directory."""
    if target == "" or target.startswith("/") or "\0" in target:
        return False
    at = path.split("/")[:-1]
    for part in target.split("/"):
        if part in ("", "."):
            continue
        if part != "..":
            at.append(part)
        elif not at:
            return False
        else:
            at.pop()
    return True


class _Node:
    """A path component: its own kind once added (None: only an implied directory)."""

    __slots__ = ("children", "kind")

    def __init__(self, kind: Kind | None) -> None:
        self.kind: Kind | None = kind
        self.children: dict[str, _Node] = {}

    def is_link(self) -> bool:
        return (self.kind or "dir") != "dir"


class PathSet:
    """The paths of one tree, added in order, as a tree of components so each add is linear in
    its path. A path already present is a duplicate. A path with a file or symlink above it, or
    a file or symlink with anything below it, is a conflict: its extraction would write through
    a link or into a file."""

    def __init__(self) -> None:
        self._root = _Node("dir")

    def _parent(self, parts: list[str]) -> _Node | None:
        """The parent directory's node, creating implied directories; None when a file or
        symlink is on the way. A file or symlink never has children, so a refusal has created
        nothing."""
        node = self._root
        for part in parts:
            if node.is_link():
                return None
            node = node.children.setdefault(part, _Node(None))
        return None if node.is_link() else node

    def add(self, path: str, kind: Kind) -> Clash | None:
        *parts, last = path.split("/")
        parent = self._parent(parts)
        if parent is None:
            return "path_conflict"
        found = parent.children.get(last)
        if found is None:
            parent.children[last] = _Node(kind)
            return None
        if found.kind is not None:
            return "duplicate"
        if kind != "dir":
            return "path_conflict"
        found.kind = kind
        return None
