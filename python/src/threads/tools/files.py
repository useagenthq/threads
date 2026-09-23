"""The file tools, one POSIX implementation over the provider contract: read,
write and edit through download and upload, ls through exec. Every failure is a typed result
the model sees, never an empty success."""

import posixpath
from typing import Final

from threads.log.digest import sha256_hex
from threads.result import Err, Ok
from threads.sandbox.protocol import SandboxError
from threads.tools.specs import EditInput, ReadInput, WriteInput

WORKSPACE: Final = "/workspace"


def absolute(path: str) -> str:
    """The sandbox path a tool argument names: relative ones are under /workspace."""
    return posixpath.normpath(posixpath.join(WORKSPACE, path))


def relative(path: str) -> str:
    """For commands run in /workspace: relative inside it, absolute outside."""
    full = absolute(path)
    inside = full == WORKSPACE or full.startswith(WORKSPACE + "/")
    return posixpath.relpath(full, WORKSPACE) if inside else full


def failure(error: SandboxError) -> str:
    return f"{error.code}: {error.message}"


def numbered(data: bytes, args: ReadInput) -> Ok[str] | Err[str]:
    """Text with 1-based line numbers, `limit` lines from `offset`."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return Err(f"too_large: {args.path} is not UTF-8 text")
    lines = text.splitlines()
    shown = lines[args.offset : args.offset + args.limit]
    body = "".join(f"{args.offset + i + 1:>6}\t{line}\n" for i, line in enumerate(shown))
    rest = len(lines) - args.offset - len(shown)
    return Ok(body + (f"[{rest} more lines]\n" if rest > 0 else ""))


def written(current: bytes | None, args: WriteInput) -> Ok[bytes] | Err[str]:
    """The new bytes, if the optimistic check holds."""
    stale = _stale(current, args.expected_sha256)
    return Err(stale) if stale is not None else Ok(args.content.encode("utf-8"))


def edited(current: bytes, args: EditInput) -> Ok[bytes] | Err[str]:
    """The file with `old_string` replaced: exactly once, or everywhere with `replace_all`."""
    stale = _stale(current, args.expected_sha256)
    if stale is not None:
        return Err(stale)
    try:
        text = current.decode("utf-8")
    except UnicodeDecodeError:
        return Err(f"too_large: {args.path} is not UTF-8 text")
    count = text.count(args.old_string)
    if count == 0:
        return Err(f"not_found: old_string does not occur in {args.path}")
    if count > 1 and not args.replace_all:
        return Err(f"ambiguous: old_string occurs {count} times; pass replace_all")
    return Ok(text.replace(args.old_string, args.new_string).encode("utf-8"))


def _stale(current: bytes | None, expected: str | None) -> str | None:
    if expected is None:
        return None
    actual = "absent" if current is None else sha256_hex(current)
    if actual == expected:
        return None
    return f"conflict: the file's sha256 is {actual}, not {expected}; read it again"
