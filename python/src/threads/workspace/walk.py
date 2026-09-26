"""Reads one host directory for a workspace (spec/schema/README.md, Workspace inputs): which
paths it copies, which it skips (.git, gitignored, deny-listed), and which `include` re-admits."""

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, assert_never

from threads.agents.config import ConfigError
from threads.log.jcs import utf16_key
from threads.sandbox.tree.paths import in_root
from threads.workspace.exclude import denied, excluded
from threads.workspace.git import ignored_paths, in_work_tree, invalid, submodules

MAX_FILES: Final = 10_000
MAX_BYTES: Final = 256 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class FoundFile:
    path: str
    mode: int
    size: int
    host: Path


@dataclass(frozen=True, slots=True)
class FoundDir:
    path: str
    mode: int = 0o755


@dataclass(frozen=True, slots=True)
class FoundSymlink:
    path: str
    target: str


type Found = FoundFile | FoundDir | FoundSymlink


@dataclass(slots=True)
class Counted:
    """The limits' running totals, shared by every input (counted after exclusions)."""

    files: int = 0
    nbytes: int = 0


type State = Literal["normal", "partial"] | int
"""How a directory's children are read: `normal`; `partial`, below a skipped directory, where
only paths on the way to an `include` get through; or, inside an included directory, the segment
count it started at, where only `.git` and deny-listed names below it are skipped."""


@dataclass(slots=True)
class Walk:
    root: Path
    label: str
    include: Sequence[str]
    ignored: frozenset[str]
    gitlinks: frozenset[str]
    used: set[str]
    counted: Counted
    found: list[Found] = field(default_factory=list[Found])
    skipped: list[str] = field(default_factory=list[str])


def check_limits(counted: Counted) -> None:
    """Raises once the inputs pass a limit."""
    advice = "point local_dir at a smaller directory, add a .gitignore, or use the git input"
    if counted.files > MAX_FILES:
        raise invalid(f"workspace: more than {MAX_FILES} files after exclusions; {advice}")
    if counted.nbytes > MAX_BYTES:
        raise invalid(f"workspace: more than 256 MiB of files after exclusions; {advice}")


def _last_denied(path: str) -> bool:
    parts = path.split("/")
    return denied(parts, len(parts) - 1)


def _below(w: Walk, path: str) -> bool:
    return any(i.startswith(f"{path}/") for i in w.include)


def _re_admit(w: Walk, path: str, state: State) -> State:
    """An `include` entry: its subtree comes in, minus `.git` and deny-listed names below it.
    The entry counts as used only if something actually skipped this path."""
    was_skipped = state == "partial" or (
        state == "normal" and (_last_denied(path) or path in w.ignored)
    )
    if was_skipped:
        w.used.add(path)
    return len(path.split("/"))


def _decide(w: Walk, path: str, state: State) -> State | None:
    """The state a path is read in, or None when it is skipped."""
    if path in w.include:
        return _re_admit(w, path, state)
    match state:
        case int():
            return None if excluded(path, state) else state
        case "partial":
            return "partial" if _below(w, path) else None
        case "normal":
            if not _last_denied(path) and path not in w.ignored:
                return "normal"
            return "partial" if _below(w, path) else None
        case _:
            assert_never(state)


def _nested(w: Walk, path: str, state: State) -> None:
    if state != "normal":
        return
    if path in w.gitlinks or (w.root / path / ".git").exists():
        raise invalid(
            f"{w.label}: {path} is a git submodule or repository; submodules aren't copied; "
            "use the git input or a vendored copy"
        )


def _admit(w: Walk, path: str, entry: os.DirEntry[str], state: State) -> None:
    host = w.root / path
    if entry.is_symlink():
        target = os.readlink(host)
        if not in_root(path, target):
            raise invalid(f"{w.label}: symlink {path} points outside it ({target})")
        w.found.append(FoundSymlink(path, target))
        return
    if entry.is_dir():
        _nested(w, path, state)
        w.found.append(FoundDir(path))
        _walk(w, path, state)
        return
    if not entry.is_file():
        raise invalid(f"{w.label}: {path} is not a regular file, directory or symlink")
    stat = entry.stat(follow_symlinks=False)
    mode = 0o644 if stat.st_mode & 0o111 == 0 else 0o755
    w.found.append(FoundFile(path, mode, stat.st_size, host))
    w.counted.files += 1
    w.counted.nbytes += stat.st_size
    check_limits(w.counted)


def _walk(w: Walk, directory: str, state: State) -> None:
    entries = sorted(os.scandir(w.root / directory), key=lambda e: e.name)
    for entry in entries:
        path = entry.name if directory == "" else f"{directory}/{entry.name}"
        nxt = None if entry.name == ".git" else _decide(w, path, state)
        if nxt is None:
            w.skipped.append(path)
        else:
            _admit(w, path, entry, nxt)


@dataclass(frozen=True, slots=True)
class Read:
    """What reading one directory gives: its paths, and the top-most ones it skipped, sorted."""

    found: tuple[Found, ...]
    skipped: tuple[str, ...]


async def read_dir(
    root: Path, label: str, include: Sequence[str], used: set[str], counted: Counted
) -> Read:
    """Reads `root` (an existing host directory) as a workspace source. `used` collects the
    include entries that re-admitted a skipped path; `counted` carries the limits across
    inputs."""
    tracked = await in_work_tree(root, label)
    w = Walk(
        root=root,
        label=label,
        include=include,
        ignored=await ignored_paths(root, label) if tracked else frozenset(),
        gitlinks=await submodules(root, label) if tracked else frozenset(),
        used=used,
        counted=counted,
    )
    _walk(w, "", "normal")
    # Sorted by UTF-16 code units, like every path list on the wire.
    return Read(tuple(w.found), tuple(sorted(w.skipped, key=utf16_key)))


__all__ = [
    "ConfigError",
    "Counted",
    "Found",
    "FoundDir",
    "FoundFile",
    "FoundSymlink",
    "Read",
    "check_limits",
    "read_dir",
]
