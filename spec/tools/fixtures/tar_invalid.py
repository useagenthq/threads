# pyright: strict
"""Archives a reader refuses with archive_invalid (spec/schema/README.md, "Snapshot manifest"):
each with the reason and the entry it names, authored from the rejection list."""

from __future__ import annotations

import tarfile
from typing import TYPE_CHECKING

from .tar_write import (
    BLOCK,
    Member,
    archive,
    b64,
    file,
    folder,
    hardlink,
    rechecksum,
    symlink,
)

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

A = file("a", b"alpha")
TOO_LONG = "d/" * 2048 + "x"  # 4097 bytes
TOO_LONG_UTF8 = "\u00e9" * 2049
B = file("b", b"bravo")


def _pax_sparse() -> bytes:
    return archive((Member("file", "s", pax=(("GNU.sparse.major", "1"),)),))


def _pax_malformed() -> bytes:
    data = archive((file("é", b"x"),))
    # The pax record "<n> path=é\n" with its length off by one.
    at = data.index(b" path=")
    return data[: at - 1] + bytes([data[at - 1] + 1]) + data[at:]


def _bad_checksum() -> bytes:
    data = archive((A, B))
    at = 2 * BLOCK  # b's header
    return data[:at] + b"c" + data[at + 1 :]


def _bad_magic() -> bytes:
    data = archive((A, B))
    at = 2 * BLOCK
    edited = data[: at + 257] + b"ustaR\x0000" + data[at + 265 :]
    return rechecksum(edited, at)


def _one_zero_block() -> bytes:
    return archive((A,))[:-BLOCK]


def _trailing() -> bytes:
    return archive((A,)) + b"\0" * 100 + b"x"


def _not_utf8() -> bytes:
    return archive((file("bad\udcff", b"x"),), tarfile.USTAR_FORMAT)


# (name, description, archive, caps or None, reason, entry)
type Case = tuple[str, str, bytes, tuple[int, int] | None, str, str | None]


def _cases() -> tuple[Case, ...]:
    ab = archive((A, B))
    return (
        ("absolute", "an absolute path", archive((file("/etc/passwd", b"x"),)), None,
         "bad_path", "/etc/passwd"),
        ("dot-dot", "a .. component", archive((file("a/../../x", b"x"),)), None,
         "bad_path", "a/../../x"),
        ("empty-component", "an empty component", archive((file("a//b", b"x"),)), None,
         "bad_path", "a//b"),
        ("dot-component", "a . component after the leading ./", archive((file("./a/./b", b"x"),)),
         None, "bad_path", "./a/./b"),
        ("root-file", "the root as a regular file", archive((file("./", b""),)), None,
         "bad_path", "./"),
        ("not-utf8", "a name that isn't UTF-8", _not_utf8(), None, "bad_path", None),
        ("path-too-long", "a pax path one byte over the 4096-byte cap",
         archive((file(TOO_LONG, b"x"),)), None, "bad_path", TOO_LONG),
        ("path-too-long-utf8", "4098 UTF-8 bytes in 2049 characters: the cap counts bytes",
         archive((file(TOO_LONG_UTF8, b"x"),)), None, "bad_path", TOO_LONG_UTF8),
        ("duplicate", "the same path twice", archive((A, A)), None, "duplicate", "a"),
        ("duplicate-dot-slash", "./a and a normalize to one path",
         archive((file("./a", b"1"), file("a", b"2"))), None, "duplicate", "a"),
        ("bad-checksum", "a header whose checksum fails", _bad_checksum(), None,
         "bad_header", None),
        ("bad-magic", "neither ustar nor GNU magic", _bad_magic(), None, "bad_header", None),
        ("pax-malformed", "a pax record whose length is wrong", _pax_malformed(), None,
         "bad_header", None),
        ("pax-sparse", "a GNU sparse pax record", _pax_sparse(), None, "bad_header", None),
        ("trailing-garbage", "a non-zero byte after the end blocks", _trailing(), None,
         "bad_header", None),
        ("truncated-data", "the stream ends inside a's data", ab[: BLOCK + 3], None,
         "truncated", "a"),
        ("truncated-header", "the stream ends inside b's header", ab[: 2 * BLOCK + 100], None,
         "truncated", None),
        ("truncated-no-end", "no end blocks", ab[: 4 * BLOCK], None, "truncated", None),
        ("truncated-one-zero", "one end block of two", _one_zero_block(), None,
         "truncated", None),
        ("empty-stream", "no bytes at all", b"", None, "truncated", None),
        ("file-cap", "a file over the lowered per-file cap", ab, (4, 1 << 30),
         "file_too_large", "a"),
        ("total-cap", "b's data ends past the lowered total cap", ab, (1 << 28, 3 * BLOCK),
         "archive_too_large", "b"),
        ("total-cap-end", "the end blocks pass the lowered total cap", ab, (1 << 28, 4 * BLOCK),
         "archive_too_large", None),
        ("device", "a character device", archive((Member("chr", "dev/null"),)), None,
         "unsupported_type", "dev/null"),
        ("fifo", "a FIFO", archive((Member("fifo", "pipe"),)), None, "unsupported_type", "pipe"),
        ("symlink-escape", "a symlink that leaves the root",
         archive((folder("d"), symlink("d/l", "../../x"))), None, "bad_symlink", "d/l"),
        ("symlink-absolute", "an absolute symlink", archive((symlink("l", "/etc"),)), None,
         "bad_symlink", "l"),
        ("hardlink-missing", "a hardlink to a missing entry", archive((hardlink("h", "nope"),)),
         None, "bad_hardlink", "h"),
        ("hardlink-later", "a hardlink to a later entry", archive((hardlink("h", "a"), A)), None,
         "bad_hardlink", "h"),
        ("hardlink-dir", "a hardlink to a directory", archive((folder("d"), hardlink("h", "d"))),
         None, "bad_hardlink", "h"),
        # 64 KiB file at bytes 0..66048, hardlink k's header ends at 66048 + 512(k + 1), and
        # k + 1 expansions have counted 65536(k + 1): the sum first passes 1 MiB at k = 14.
        ("hardlink-amplified", "one file and 2,000 hardlinks to it: the expanded sizes pass "
         "the lowered total cap long before the stream does",
         archive((file("f", bytes(65536)),
                  *(hardlink(f"h{k:04}", "f") for k in range(2000)))),
         (1 << 28, 1 << 20), "archive_too_large", "h0014"),
        ("below-file", "an entry below a file", archive((A, file("a/x", b"x"))), None,
         "path_conflict", "a/x"),
        ("below-symlink", "an entry below a symlink",
         archive((symlink("l", "d"), file("l/x", b"x"))), None, "path_conflict", "l/x"),
        ("file-over-dir", "a file where an earlier entry made a directory",
         archive((file("d/x", b"x"), file("d", b"d"))), None, "path_conflict", "d"),
    )  # fmt: skip


def cases() -> list[JsonValue]:
    out: list[JsonValue] = []
    for name, description, data, caps, reason, entry in _cases():
        case: Obj = {"name": name, "description": description, "tar": b64(data)}
        if caps is not None:
            case["caps"] = {"file": caps[0], "total": caps[1]}
        out.append({**case, "reason": reason, "entry": entry})
    return out
