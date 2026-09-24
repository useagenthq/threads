"""The race rules that keep a shared artifact from being lost to a concurrent `threads gc`
(spec/schema/README.md, "Artifacts"): gc only ever unlinks a trash name, and whoever needs the
file links it back. Shared by the store (artifacts.py) and gc (retention.py)."""

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from threads.log.digest import sha256_hex

type SeamPoint = Literal["put:exists", "put:verify", "gc:stated", "gc:renamed"]
"""The file-system steps a test can interleave with."""


@dataclass(slots=True)
class Seams:
    """Test-only: `hook` runs at each seam point, so a test forces one interleaving
    deterministically; `rules = False` restores the old behaviour (no refresh, re-link, trash
    or restore) for the control tests. Never set outside tests."""

    hook: Callable[[SeamPoint], None] | None = None
    rules: bool = True


SEAMS: Final = Seams()


def seam(point: SeamPoint) -> None:
    if SEAMS.hook is not None:
        SEAMS.hook(point)


def trash_prefix(sha256: str) -> str:
    """`.<sha256>.trash-`: the prefix of a trash name in the artifact's own directory."""
    return f".{sha256}.trash-"


def trash_name(sha256: str) -> str:
    """A fresh trash name: renaming to it is atomic and keeps the inode and mtime."""
    return f"{trash_prefix(sha256)}{uuid.uuid4().hex}"


def link_back(source: Path, target: Path) -> bool:
    """Links `source` to `target`; True when `target` exists afterwards, False when `source`
    vanished."""
    try:
        os.link(source, target)
    except FileExistsError:
        return True
    except FileNotFoundError:
        return False
    return True


def read_if_present(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def restore_from_trash(path: Path) -> bytes | None:
    """Rule 3: a reader that finds no file restores it from a trash name that verifies, linked
    back to the content path (the link shares the inode, so gc unlinking the trash later keeps
    it). Returns the bytes, or None when no trash holds them."""
    if not SEAMS.rules or not path.parent.is_dir():
        return None
    sha = path.name
    for trash in sorted(path.parent.glob(f"{trash_prefix(sha)}*")):
        data = read_if_present(trash)
        if data is not None and sha256_hex(data) == sha and link_back(trash, path):
            return data
    return None
