"""Every host-side file operation of the dev sandbox walks the path with `openat`, one component
at a time, each opened `O_NOFOLLOW`, so no symlink is ever traversed. A lexical check can't do
this: `a/b -> ..` followed by `c -> a/b/..` leaves the root through two links that each look
harmless. A symlink is only ever read as a link here, never traversed. Inside the confinement the
command may follow links freely: that is the sandbox's own filesystem.
"""

import errno
import os
import posixpath
import stat
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Literal

from threads.result import Err, Ok
from threads.sandbox.protocol import SandboxError, SandboxErrorCode

WORKSPACE = "/workspace"

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

_CODES: dict[int, SandboxErrorCode] = {
    errno.ENOENT: "not_found",
    errno.ENOTDIR: "not_found",
    errno.EISDIR: "is_directory",
    errno.ELOOP: "invalid_path",
    errno.EMLINK: "invalid_path",
    errno.EACCES: "permission_denied",
    errno.EPERM: "permission_denied",
    errno.EFBIG: "too_large",
}


def failed(error: OSError, path: str) -> SandboxError:
    """A host errno as the protocol's file failure."""
    code = _CODES.get(error.errno or 0, "unavailable")
    return SandboxError(code, f"{path}: {errno.errorcode.get(error.errno or 0, error.strerror)}")


def _linked(path: str) -> SandboxError:
    return SandboxError("invalid_path", f"{path}: a symlink is never followed on the host")


def workspace_parts(path: str) -> Ok[tuple[str, ...]] | Err[SandboxError]:
    """The components of `path` under /workspace, or a typed failure: a path outside /workspace is
    outside the sandbox, because the dev sandbox's filesystem is the host's."""
    if "\0" in path or not path.startswith("/"):
        return Err(SandboxError("invalid_path", f"not an absolute path: {path}"))
    at = posixpath.normpath(path)
    if at != WORKSPACE and not at.startswith(f"{WORKSPACE}/"):
        return Err(SandboxError("invalid_path", f"{path}: the dev sandbox holds only {WORKSPACE}"))
    return Ok(tuple(part for part in at[len(WORKSPACE) :].split("/") if part))


def _is_link(fd: int, name: str) -> bool:
    """Whether `name` in `fd` is a symlink. O_NOFOLLOW's errno differs by platform (ELOOP on
    Linux, ENOTDIR with O_DIRECTORY on macOS), so the refusal is named from the link itself."""
    try:
        return stat.S_ISLNK(os.lstat(name, dir_fd=fd).st_mode)
    except OSError:
        return False


def _child_dir(fd: int, name: str, at: str, create: bool) -> Ok[int] | Err[SandboxError]:
    """An fd on `name` inside `fd`, refusing a symlink; makes it when `create`."""
    try:
        return Ok(os.open(name, _DIR_FLAGS, dir_fd=fd))
    except OSError as error:
        if _is_link(fd, name):
            return Err(_linked(at))
        if error.errno == errno.ENOENT and create:
            try:
                os.mkdir(name, 0o755, dir_fd=fd)
                return Ok(os.open(name, _DIR_FLAGS, dir_fd=fd))
            except OSError as made:
                return Err(failed(made, at))
        return Err(failed(error, at))


@dataclass(frozen=True, slots=True)
class Walked:
    """The directory holding the last component, and that component's name."""

    fd: int
    name: str


@contextmanager
def walked(
    directory: str, parts: Sequence[str], create: bool
) -> Generator[Ok[Walked] | Err[SandboxError]]:
    """`directory` walked down to the parent of `parts[-1]`, every component opened
    `O_NOFOLLOW`. The fd is closed when the block ends."""
    try:
        fd = os.open(directory, _DIR_FLAGS)
    except OSError as error:
        yield Err(failed(error, directory))
        return
    at = directory
    try:
        for part in parts[:-1]:
            at = f"{at}/{part}"
            child = _child_dir(fd, part, at, create)
            if isinstance(child, Err):
                yield child
                return
            os.close(fd)
            fd = child.value
        yield Ok(Walked(fd, parts[-1] if parts else "."))
    finally:
        os.close(fd)


def host_dir(directory: str, parts: Sequence[str]) -> Ok[str] | Err[SandboxError]:
    """The host path of a directory under `directory`, every component of it proven to be a real
    directory. An exec's cwd goes through this, so no symlink chain ever chooses where a command
    starts."""
    with walked(directory, (*parts, "."), create=False) as found:
        if isinstance(found, Err):
            return found
        return Ok(os.path.join(directory, *parts))


def read_in(directory: str, parts: Sequence[str]) -> Ok[bytes] | Err[SandboxError]:
    """The file's bytes, refusing a symlink at any component."""
    with walked(directory, parts, create=False) as found:
        if isinstance(found, Err):
            return found
        at = "/".join((directory, *parts))
        try:
            fd = os.open(found.value.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=found.value.fd)
        except OSError as error:
            if _is_link(found.value.fd, found.value.name):
                return Err(_linked(at))
            return Err(failed(error, at))
        try:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                return Err(SandboxError("is_directory", at))
            return Ok(_read_all(fd))
        except OSError as error:
            return Err(failed(error, at))
        finally:
            os.close(fd)


def _read_all(fd: int) -> bytes:
    parts: list[bytes] = []
    while chunk := os.read(fd, 1 << 20):
        parts.append(chunk)
    return b"".join(parts)


def write_in(
    directory: str, parts: Sequence[str], data: bytes, mode: int
) -> Ok[None] | Err[SandboxError]:
    """Writes the file, making missing parents, refusing a symlink at any component."""
    with walked(directory, parts, create=True) as found:
        if isinstance(found, Err):
            return found
        at = "/".join((directory, *parts))
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
        try:
            fd = os.open(found.value.name, flags, mode, dir_fd=found.value.fd)
        except OSError as error:
            if _is_link(found.value.fd, found.value.name):
                return Err(_linked(at))
            return Err(failed(error, at))
        try:
            os.write(fd, data)
            return Ok(None)
        except OSError as error:
            return Err(failed(error, at))
        finally:
            os.close(fd)


def mkdir_in(directory: str, parts: Sequence[str], mode: int) -> Ok[None] | Err[SandboxError]:
    """Makes the directory, making missing parents, refusing a symlink at any component."""
    with walked(directory, parts, create=True) as found:
        if isinstance(found, Err):
            return found
        try:
            os.mkdir(found.value.name, mode, dir_fd=found.value.fd)
        except FileExistsError:
            return Ok(None)
        except OSError as error:
            return Err(failed(error, "/".join((directory, *parts))))
        return Ok(None)


def symlink_in(directory: str, parts: Sequence[str], target: str) -> Ok[None] | Err[SandboxError]:
    """Makes the symlink, refusing a symlink at any component of its own path."""
    with walked(directory, parts, create=True) as found:
        if isinstance(found, Err):
            return found
        try:
            os.symlink(target, found.value.name, dir_fd=found.value.fd)
        except OSError as error:
            return Err(failed(error, "/".join((directory, *parts))))
        return Ok(None)


@dataclass(frozen=True, slots=True)
class Found:
    """One thing in the tree, by its path relative to the sandbox's /workspace."""

    path: str
    kind: Literal["dir", "file", "symlink"]
    mode: int = 0
    size: int = 0
    target: str = ""


def walk_tree(directory: str) -> Ok[tuple[Found, ...]] | Err[SandboxError]:
    """Everything under `directory`, sorted by path, never descending into a symlink: each level
    is listed through its own `O_NOFOLLOW` fd."""
    out: list[Found] = []
    queue: list[tuple[str, ...]] = [()]
    while queue:
        prefix = queue.pop(0)
        listed = _level(directory, prefix, out)
        if isinstance(listed, Err):
            return listed
        queue.extend((*prefix, name) for name in listed.value)
    return Ok(tuple(sorted(out, key=lambda e: e.path)))


def _level(
    directory: str, prefix: Sequence[str], out: list[Found]
) -> Ok[tuple[str, ...]] | Err[SandboxError]:
    """One directory's entries appended to `out`; the names of its subdirectories."""
    with walked(directory, (*prefix, "."), create=False) as found:
        if isinstance(found, Err):
            return found
        fd = found.value.fd
        at = "/".join((directory, *prefix))
        try:
            names = sorted(os.listdir(fd))
        except OSError as error:
            return Err(failed(error, at))
        dirs: list[str] = []
        for name in names:
            entry = _entry(fd, prefix, name, at)
            if isinstance(entry, Err):
                return entry
            out.append(entry.value)
            if entry.value.kind == "dir":
                dirs.append(name)
        return Ok(tuple(dirs))


def _entry(fd: int, prefix: Sequence[str], name: str, at: str) -> Ok[Found] | Err[SandboxError]:
    path = "/".join((*prefix, name))
    try:
        info = os.lstat(name, dir_fd=fd)
        if stat.S_ISLNK(info.st_mode):
            return Ok(Found(path, "symlink", target=os.readlink(name, dir_fd=fd)))
    except OSError as error:
        return Err(failed(error, f"{at}/{name}"))
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISDIR(info.st_mode):
        return Ok(Found(path, "dir", mode=mode))
    if stat.S_ISREG(info.st_mode):
        return Ok(Found(path, "file", mode=mode, size=info.st_size))
    return Err(SandboxError("invalid_path", f"{at}/{name}: not a file, directory or symlink"))
