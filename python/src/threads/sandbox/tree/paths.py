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


def is_normal(path: str) -> bool:
    """Every component is a real name: no empty, `.` or `..` component, and no NUL."""
    return "\0" not in path and all(c not in ("", ".", "..") for c in path.split("/"))


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


class PathSet:
    """The paths of one tree, added in order. A path already present is a duplicate. A path
    with a file or symlink above it, or a file or symlink with anything below it, is a
    conflict: its extraction would write through a link or into a file."""

    def __init__(self) -> None:
        self._kinds: dict[str, Kind] = {}
        self._parents: set[str] = set()

    def add(self, path: str, kind: Kind) -> Clash | None:
        if path in self._kinds:
            return "duplicate"
        if kind != "dir" and path in self._parents:
            return "path_conflict"
        parts = path.split("/")
        above = ["/".join(parts[: i + 1]) for i in range(len(parts) - 1)]
        if any(self._kinds.get(p, "dir") != "dir" for p in above):
            return "path_conflict"
        self._parents.update(above)
        self._kinds[path] = kind
        return None
