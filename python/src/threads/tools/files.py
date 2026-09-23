"""The file tools, one POSIX implementation over the provider contract: read,
write and edit through download and upload. Every failure is a typed result the model sees
, never an empty success; a failed check changes nothing."""

import posixpath
from typing import Final

from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.tools_v1 import EditInput, ReadInput
from threads.log.digest import sha256_hex
from threads.result import Err, Ok

WORKSPACE: Final = "/workspace"


def absolute(path: str) -> str:
    """The sandbox path a tool argument names: relative ones are under /workspace."""
    return posixpath.normpath(posixpath.join(WORKSPACE, path))


def text(data: bytes, path: str) -> Ok[str] | Err[str]:
    try:
        return Ok(data.decode("utf-8"))
    except UnicodeDecodeError:
        return Err(f"{path} is not UTF-8 text")


def numbered(body: str, args: ReadInput) -> str:
    """`limit` lines from the 1-based `offset`, each prefixed with its number and a tab."""
    lines = body.split("\n")[args.offset - 1 : args.offset - 1 + args.limit]
    return "\n".join(f"{args.offset + i}\t{line}" for i, line in enumerate(lines))


def stale(current: bytes | None, expected: str | MISSING) -> str | None:
    """An expected_sha256 that doesn't match the file as it is now."""
    if expected is MISSING:
        return None
    actual = "absent" if current is None else sha256_hex(current)
    if actual == expected:
        return None
    return f"expected_sha256 mismatch: the file is {actual}; nothing written"


def edited(body: str, args: EditInput) -> Ok[str] | Err[str]:
    """`old_string` replaced: exactly once, or everywhere with `replace_all`."""
    count = body.count(args.old_string)
    if count == 0:
        return Err("old_string not found; nothing written")
    if count > 1 and args.replace_all is not True:
        return Err(f"old_string matches {count} times; nothing written")
    return Ok(body.replace(args.old_string, args.new_string))
