# pyright: strict
"""The archive and tree vectors (spec/schema/README.md, "Snapshot manifest"):
vectors/manifest-tar.json holds valid archives and the tree each must read as; vectors/tree.json
holds each tree's artifact bytes, their sha256 and the manifest hash, plus tree artifacts a
reader must refuse. Expected trees are authored from the members written, never read from an
implementation."""

from __future__ import annotations

import tarfile
from typing import TYPE_CHECKING

from .common import CASES, sha
from .jcs import canonical
from .pieces import dump
from .tar_invalid import cases as invalid_cases
from .tar_write import Member, archive, b64, file, folder, hardlink, symlink

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

TAR = CASES.parent / "vectors" / "manifest-tar.json"
TREE = CASES.parent / "vectors" / "tree.json"
INVALID = CASES.parent / "vectors" / "archive-invalid.json"

LONG = "deep/" * 24 + "leaf.txt"  # 128 bytes: ustar's prefix field, pax elsewhere
LONG_TARGET = "x/" + "../x/" * 25 + "t"
AT_CAP = "d/" * 2047 + "xy"  # 4096 bytes

# (name, description, format, record padding kept, members)
VALID: tuple[tuple[str, str, int, bool, tuple[Member, ...]], ...] = (
    (
        "ustar",
        "POSIX ustar as GNU tar -C dir . writes it: ./ prefixes, the ./ root, 0o755, "
        "a setuid mode, an empty file, a UTF-8 name, a long path in the prefix field",
        tarfile.USTAR_FORMAT,
        False,
        (
            folder("./", 0o700),
            file("./a.txt", b"hello\n"),
            folder("./bin/"),
            file("./bin/run.sh", b"#!/bin/sh\necho hi\n", 0o755),
            file("./bin/su", b"x", 0o4755),
            file("./empty", b""),
            file("./naïve.txt", b"caf\xc3\xa9"),
            file(LONG, b"deep"),
        ),
    ),
    (
        "pax",
        "pax records for non-ASCII, spaced, backslash and long names; no ./ prefixes",
        tarfile.PAX_FORMAT,
        False,
        (
            folder("café"),
            file("café/ñ.txt", b"\xc3\xb1"),
            file("my file.txt", b"spaced"),
            file("日本語", b"nihongo"),
            file("\U0001f600", b"smile"),
            file("", b"private use"),
            file("a\\b", b"backslash"),
            file(LONG, b"deep"),
        ),
    ),
    (
        "links",
        "a hardlink expands to its earlier target's content and mode; in-root symlinks, "
        "one relative to its directory and one with a long target, are kept",
        tarfile.PAX_FORMAT,
        False,
        (
            file("./a", b"shared", 0o600),
            hardlink("./b", "./a"),
            folder("./sub/"),
            hardlink("./sub/c", "a"),
            symlink("./link", "a"),
            symlink("./sub/up", "../a"),
            symlink("./x/long", LONG_TARGET),
        ),
    ),
    (
        "gnu",
        "GNU format: a long name and a long link target in ././@LongLink entries",
        tarfile.GNU_FORMAT,
        False,
        (file(LONG, b"deep"), symlink("s/" + "n" * 110, LONG_TARGET)),
    ),
    (
        "padded",
        "tarfile's record padding after the two zero blocks: trailing zeros are allowed",
        tarfile.PAX_FORMAT,
        True,
        (file("only", b"1"),),
    ),
    ("empty", "no entries, only the two zero blocks", tarfile.PAX_FORMAT, False, ()),
    (
        "path-cap",
        "a path of exactly 4096 UTF-8 bytes, the cap",
        tarfile.PAX_FORMAT,
        False,
        (file(AT_CAP, b"deepest"),),
    ),
)


def _path(name: str) -> str:
    path = name.removeprefix("./")
    return path.removesuffix("/")


def tree_of(members: tuple[Member, ...]) -> list[Obj]:
    """The tree a valid archive reads as, from the rules: root skipped, hardlinks expanded,
    sorted by path in UTF-16 code units."""
    entries: dict[str, Obj] = {}
    for m in members:
        path = _path(m.name)
        if path in ("", "."):
            continue
        if m.kind == "file":
            entries[path] = {
                "path": path,
                "kind": "file",
                "mode": m.mode,
                "size": len(m.data),
                "sha256": sha(m.data),
            }
        elif m.kind == "dir":
            entries[path] = {"path": path, "kind": "dir", "mode": m.mode}
        elif m.kind == "symlink":
            entries[path] = {"path": path, "kind": "symlink", "target": m.link}
        else:
            entries[path] = {**entries[_path(m.link)], "path": path}
    return [entries[p] for p in sorted(entries, key=lambda p: p.encode("utf-16-be"))]


def manifest_hash(entries: list[Obj]) -> str:
    files: list[JsonValue] = [
        {k: e[k] for k in ("path", "mode", "size", "sha256")}
        for e in entries
        if e["kind"] == "file"
    ]
    return sha(canonical(files))


def _tree_bytes(entries: list[Obj]) -> bytes:
    body: list[JsonValue] = [*entries]
    return canonical({"tree_version": 1, "entries": body})


def _tar_vector() -> str:
    cases: list[JsonValue] = [
        {
            "name": name,
            "description": description,
            "tar": b64(archive(members, fmt, pad)),
            "entries": [*tree_of(members)],
        }
        for name, description, fmt, pad, members in VALID
    ]
    return dump(
        {
            "description": (
                "Valid tar archives (base64) and the tree entries each must read as, "
                "sorted by path in UTF-16 code units (spec/schema/README.md, Snapshot manifest). "
                "Parse each from several chunkings; every chunking must give the same tree."
            ),
            "cases": cases,
        }
    )


# (name, tree artifact text) a tree reader must refuse as artifact_corrupt.
_A: Obj = {"path": "a", "kind": "dir", "mode": 493}
_B: Obj = {"path": "b", "kind": "dir", "mode": 493}
BAD_TREES: tuple[tuple[str, str], ...] = (
    ("out of order", _tree_bytes([_B, _A]).decode()),
    ("duplicate path", _tree_bytes([_A, _A]).decode()),
    ("not canonical", '{"tree_version":1, "entries":[]}'),
    ("unknown version", '{"entries":[],"tree_version":2}'),
    ("dot-dot path", _tree_bytes([{**_A, "path": "a/../b"}]).decode()),
    (
        "escaping symlink",
        _tree_bytes([{"path": "l", "kind": "symlink", "target": "../x"}]).decode(),
    ),
    (
        "entry below a symlink",
        _tree_bytes(
            [{"path": "l", "kind": "symlink", "target": "a"}, {**_A, "path": "l/x"}]
        ).decode(),
    ),
    ("mode out of range", _tree_bytes([{**_A, "mode": 4096}]).decode()),
    ("path over 4096 bytes", _tree_bytes([{**_A, "path": "a" * 4097}]).decode()),
)


def _tree_vector() -> str:
    cases: list[JsonValue] = []
    for name, _, _, _, members in VALID:
        entries = tree_of(members)
        data = _tree_bytes(entries)
        cases.append(
            {
                "name": name,
                "tree": data.decode(),
                "sha256": sha(data),
                "manifest_hash": manifest_hash(entries),
            }
        )
    invalid: list[JsonValue] = [{"name": n, "tree": t} for n, t in BAD_TREES]
    return dump(
        {
            "description": (
                "Tree artifacts for the manifest-tar.json cases of the same name: the RFC 8785 "
                "bytes, their sha256 and the manifest hash of the file entries. `invalid` are "
                "tree artifacts a reader refuses (artifact_corrupt)."
            ),
            "cases": cases,
            "invalid": invalid,
        }
    )


def _invalid_vector() -> str:
    return dump(
        {
            "description": (
                "Archives (base64) a reader refuses with archive_invalid: the reason and the "
                "entry it names (null when no entry name was read). `caps` lowers the "
                "per-file and total caps; absent, they are 256 MiB and 1 GiB."
            ),
            "cases": invalid_cases(),
        }
    )


def _vectors() -> tuple[tuple[str, str], ...]:
    return (
        (str(TAR), _tar_vector()),
        (str(TREE), _tree_vector()),
        (str(INVALID), _invalid_vector()),
    )


def write() -> None:
    for path, text in _vectors():
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


def check() -> list[str]:
    problems: list[str] = []
    for path, text in _vectors():
        try:
            with open(path, encoding="utf-8") as f:
                current = f.read()
        except FileNotFoundError:
            current = ""
        if current != text:
            problems.append(f"{path.rsplit('/', 1)[-1]}: differs; run gen_fixtures.py")
    return problems
