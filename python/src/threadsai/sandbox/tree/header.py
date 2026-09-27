"""One tar header block and one pax extended header, parsed strictly (spec/schema/README.md,
"Snapshot manifest"). Only the fields the tree uses are read: name, mode, size, checksum,
typeflag, linkname, magic and the ustar prefix."""

import re
from dataclasses import dataclass, replace

BLOCK = 512

_OCTAL = re.compile(rb"[0-7]{1,12}")
_DECIMAL = re.compile(rb"[0-9]{1,15}")
_POSIX = b"ustar\x0000"
_GNU = b"ustar  \x00"
_MODED = frozenset((b"0", b"\0", b"5"))
_CHECKSUM = slice(148, 156)


@dataclass(frozen=True, slots=True)
class Header:
    """A parsed header. `mode` is read for regular files and directories only (0 otherwise)."""

    type: bytes
    name: bytes
    link: bytes
    size: int
    mode: int


@dataclass(frozen=True, slots=True)
class Pax:
    """The pax records the tree uses; an empty value is the same as an absent one."""

    path: bytes | None = None
    linkpath: bytes | None = None
    size: int | None = None


def is_zero(block: bytes) -> bool:
    return not any(block)


def _cstring(block: bytes, at: int, length: int) -> bytes:
    """A NUL-terminated field's bytes."""
    return block[at : at + length].split(b"\0", 1)[0]


def _octal(block: bytes, at: int, length: int) -> int | None:
    """An octal number field: optional leading spaces, digits, then NULs or spaces."""
    text = block[at : at + length].rstrip(b"\0 ").lstrip(b" ")
    return int(text, 8) if _OCTAL.fullmatch(text) else None


def _checksum(block: bytes) -> int:
    return sum(block) - sum(block[_CHECKSUM]) + 8 * 0x20


def parse_header(block: bytes) -> Header | None:
    """The header block's fields; None when malformed or its checksum fails. POSIX ustar
    joins the prefix field to the name; GNU has no prefix field."""
    magic = block[257:265]
    size = _octal(block, 124, 12)
    if magic not in (_POSIX, _GNU) or size is None:
        return None
    if _octal(block, 148, 8) != _checksum(block):
        return None
    kind = block[156:157]
    # Links and pax headers carry no mode the tree keeps.
    mode = _octal(block, 100, 8) if kind in _MODED else 0
    if mode is None:
        return None
    name = _cstring(block, 0, 100)
    prefix = _cstring(block, 345, 155) if magic == _POSIX else b""
    return Header(
        type=kind,
        name=prefix + b"/" + name if prefix else name,
        link=_cstring(block, 157, 100),
        size=size,
        mode=mode & 0o7777,
    )


def _records(data: bytes) -> list[tuple[bytes, bytes]] | None:
    """`<length> <key>=<value>\\n` records, the length counting the whole record."""
    out: list[tuple[bytes, bytes]] = []
    at = 0
    while at < len(data):
        space = data.find(b" ", at)
        digits = b"" if space == -1 else data[at:space]
        if not _DECIMAL.fullmatch(digits):
            return None
        end = at + int(digits)
        if end > len(data) or end <= space + 1 or data[end - 1 : end] != b"\n":
            return None
        key, equals, value = data[space + 1 : end - 1].partition(b"=")
        if not equals or not key:
            return None
        out.append((key, value))
        at = end
    return out


def parse_pax(data: bytes) -> Pax | None:
    """A pax extended header's records; None when malformed or sparse (GNU.sparse.*)."""
    found = _records(data)
    if found is None:
        return None
    pax = Pax()
    for key, value in found:
        if key.startswith(b"GNU.sparse."):
            return None
        if key == b"path":
            pax = replace(pax, path=value or None)
        elif key == b"linkpath":
            pax = replace(pax, linkpath=value or None)
        elif key == b"size":
            if value and not _DECIMAL.fullmatch(value):
                return None
            pax = replace(pax, size=int(value) if value else None)
    return pax
