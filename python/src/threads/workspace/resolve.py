"""Resolves `agent(workspace=...)` into one tree on the host (spec/schema/README.md, Workspace
inputs): every input read, excluded, checked for registered secrets and collisions, each file and
the tree kept as artifacts, and the pin that names them. Every error is ConfigError."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from threads._generated.events_v1 import (
    Tree as TreeRef,
)
from threads._generated.events_v1 import (
    WorkspacePin,
    WorkspaceSource,
    WorkspaceSource1,
    WorkspaceSource2,
    WorkspaceSource3,
)
from threads.agents.config import ConfigError
from threads.log.digest import sha256_hex
from threads.log.jcs import utf16_key
from threads.redaction.stored import contains_secret
from threads.sandbox.tree.paths import Kind, PathSet, is_normal
from threads.sandbox.tree.tree import (
    Tree,
    TreeDir,
    TreeEntry,
    TreeFile,
    TreeSymlink,
    encode_tree,
    tree_manifest_hash,
)
from threads.workspace.git import checkout, invalid
from threads.workspace.walk import (
    Counted,
    Found,
    FoundDir,
    FoundFile,
    FoundSymlink,
    check_limits,
    read_dir,
)


class WorkspaceGit(TypedDict):
    repo: str
    """owner/name on the forge of the agent's git option."""
    ref: str
    """A branch, tag or commit; resolved to a commit when the thread starts."""


class Workspace(TypedDict, total=False):
    """agent(workspace=...): what every sandbox this thread creates starts with in /workspace."""

    files: Mapping[str, str | bytes]
    """Tree path -> contents (text as UTF-8), mode 0o644, taken as given."""
    local_dir: str
    """A host directory, read on the host and pinned exactly as written."""
    git: WorkspaceGit
    """A repository on the forge of the agent's git option, fetched on the host."""
    include: Sequence[str]
    """Skipped tree paths to re-admit, one by one; a directory re-admits its subtree."""


def check_workspace(
    workspace: "Workspace | None", sandbox: object, pin: "WorkspacePin | None"
) -> None:
    """A workspace needs a sandbox, and is pinned only once a run or check() resolved it."""
    if workspace is None:
        return
    if sandbox is None:
        raise ConfigError("capability_missing", "workspace: needs a sandbox to place the files in")
    if pin is None:
        raise ConfigError(
            "invalid_config",
            "workspace: its files are read on the host by check() or a run; "
            "this pin can't see them",
        )


type Keep = Callable[[bytes], Awaitable[object]]
"""Keeps one artifact's bytes: the store's put_artifact, or a no-op for check()."""


@dataclass(frozen=True, slots=True)
class Forge:
    """Where a workspace git input fetches from, and with what."""

    url: Callable[[str], str]
    token: str | None = None


@dataclass(frozen=True, slots=True)
class Resolved:
    """The pin the thread carries, and the tree each new sandbox is given."""

    pin: WorkspacePin
    tree: Tree


@dataclass(frozen=True, slots=True)
class _Pending:
    """A tree path and where its bytes come from, before they are read."""

    path: str
    mode: int
    load: Callable[[], bytes]


type _Entry = _Pending | FoundDir | FoundSymlink


def _check_includes(include: Sequence[str]) -> None:
    for path in include:
        if not is_normal(path) or ".git" in path.split("/"):
            raise invalid(f"workspace include {path!r}: a normal tree path outside .git")


def _from_files(files: Mapping[str, str | bytes], counted: Counted) -> list[_Entry]:
    out: list[_Entry] = []
    for path, value in files.items():
        if not is_normal(path):
            raise invalid(f"workspace files: {path!r} is not a normal tree path")
        data = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        counted.files += 1
        counted.nbytes += len(data)
        out.append(_Pending(path, 0o644, lambda d=data: d))
    return out


def _pending(found: Sequence[Found]) -> list[_Entry]:
    out: list[_Entry] = []
    for f in found:
        if isinstance(f, FoundFile):
            out.append(_Pending(f.path, f.mode, lambda host=f.host: host.read_bytes()))
        else:
            out.append(f)
    return out


async def _from_local(
    path: str, include: Sequence[str], used: set[str], counted: Counted
) -> tuple[WorkspaceSource, list[_Entry]]:
    label = f"workspace local_dir {path}"
    root = Path(path).resolve()
    if not root.is_dir() or root.is_symlink():
        raise invalid(f"{label} is not a readable directory")
    read = await read_dir(root, label, include, used, counted)
    return WorkspaceSource2(kind="local_dir", path=path, skipped=read.skipped), _pending(read.found)


async def _from_git(
    git: WorkspaceGit, forge: Forge, include: Sequence[str], used: set[str], counted: Counted
) -> tuple[WorkspaceSource, list[_Entry]]:
    repo, ref = git["repo"], git["ref"]
    label = f"workspace git {repo}@{ref}"
    async with checkout(forge.url, repo, ref, forge.token) as (work, commit):
        read = await read_dir(work, label, include, used, counted)
        # The checkout is removed when this returns, so its files are read now.
        # ponytail: held in memory (bounded by the 256 MiB limit); spill to disk if that bites.
        found: list[_Entry] = []
        for entry in _pending(read.found):
            if not isinstance(entry, _Pending):
                found.append(entry)
                continue
            data = entry.load()
            found.append(_Pending(entry.path, entry.mode, lambda d=data: d))
        source = WorkspaceSource3(
            kind="git", repo=repo, ref=ref, commit=commit, skipped=read.skipped
        )
        return source, found


def _kind(e: _Entry) -> Kind:
    if isinstance(e, _Pending):
        return "file"
    return "dir" if isinstance(e, FoundDir) else "symlink"


def _merged(entries: Sequence[_Entry]) -> list[_Entry]:
    """The entries sorted, each path once and never under a file or symlink."""
    out = sorted(entries, key=lambda e: utf16_key(e.path))
    paths = PathSet()
    for e in out:
        clash = paths.add(e.path, _kind(e))
        if clash is not None:
            extra = "" if clash == "duplicate" else ", or a file where another has a directory"
            raise invalid(f"workspace: two inputs give {e.path}{extra}")
    return out


async def _loaded(entries: Sequence[_Entry], keep: Keep) -> Tree:
    out: list[TreeEntry] = []
    for e in entries:
        if isinstance(e, FoundDir):
            out.append(TreeDir(kind="dir", path=e.path, mode=e.mode))
            continue
        if isinstance(e, FoundSymlink):
            out.append(TreeSymlink(kind="symlink", path=e.path, target=e.target))
            continue
        data = e.load()
        if contains_secret(data):
            raise invalid(
                f"workspace file {e.path} contains a secret value; remove it or exclude the file"
            )
        await keep(data)
        out.append(
            TreeFile(kind="file", path=e.path, mode=e.mode, size=len(data), sha256=sha256_hex(data))
        )
    return Tree(tree_version=1, entries=out)


async def resolve_workspace(ws: Workspace, forge: Forge, keep: Keep) -> Resolved:
    """Resolves the workspace into its pin, keeping each file and the tree with `keep` first.
    Runs after setup, so every secret the agent resolves is registered. Raises ConfigError."""
    include = list(ws.get("include", ()))
    _check_includes(include)
    used: set[str] = set()
    counted = Counted()
    sources: list[WorkspaceSource] = []
    entries: list[_Entry] = []
    files = ws.get("files")
    if files is not None:
        sources.append(WorkspaceSource1(kind="files"))
        entries += _from_files(files, counted)
        check_limits(counted)
    local_dir = ws.get("local_dir")
    if local_dir is not None:
        source, found = await _from_local(local_dir, include, used, counted)
        sources.append(source)
        entries += found
    git = ws.get("git")
    if git is not None:
        source, found = await _from_git(git, forge, include, used, counted)
        sources.append(source)
        entries += found
    unused = next((i for i in include if i not in used), None)
    if unused is not None:
        raise invalid(f"workspace include {unused} re-admits nothing: no input skipped it")
    tree = await _loaded(_merged(entries), keep)
    data = encode_tree(tree)
    await keep(data)
    pin = WorkspacePin(
        tree=TreeRef(sha256=sha256_hex(data), bytes=len(data)),
        manifest_hash=tree_manifest_hash(tree),
        sources=sources,
    )
    return Resolved(pin, tree)


__all__ = [
    "Forge",
    "Keep",
    "Resolved",
    "Workspace",
    "WorkspaceGit",
    "WorkspacePin",
    "check_workspace",
    "resolve_workspace",
]
