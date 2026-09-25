"""Retention: the artifact sweep, never automatic (deletion is store/deletion.py). The sweep
keeps every artifact any stored line or branch row names.
"""

import os
import re
import time
from pathlib import Path
from typing import Final

from threads.store.conn import Conn
from threads.store.sql import blob_of, text_of
from threads.store.trash import SEAMS, link_back, seam, trash_name

GRACE_S: Final = 7 * 24 * 3600
"""An unreferenced artifact younger than this may belong to an append in flight: kept."""
_SHA256: Final = re.compile(rb"[0-9a-f]{64}")
_TRASH: Final = re.compile(r"\.([0-9a-f]{64})\.trash-")
_PER_BRANCH: Final = ("events", "leases", "observer_cursors")


def referenced(conn: Conn) -> frozenset[str]:
    """Every sha256 any stored line or branch row names: the mark of the sweep. A hash-shaped
    string that is not an artifact only keeps a file that doesn't exist."""
    found: set[str] = set()
    for (line,) in conn.execute("SELECT line FROM events").fetchall():
        found.update(m.decode() for m in _SHA256.findall(blob_of(line)))
    rows = conn.execute("SELECT header_line, dropped_ref FROM branches").fetchall()
    for header, dropped in rows:
        found.update(m.decode() for m in _SHA256.findall(blob_of(header)))
        if dropped is not None:
            found.add(text_of(dropped))
    return frozenset(found)


def sweep(root: Path, keep: frozenset[str], grace_s: float = GRACE_S) -> tuple[str, ...]:
    """Removes unreferenced artifacts older than the grace period; returns their hashes. A file
    is only ever moved to a trash name, re-checked and then unlinked by that name, so a
    concurrent put or get can restore it (spec/schema/README.md, "Artifacts", rule 2)."""
    removed: list[str] = []
    cutoff = time.time() - grace_s
    for path in sorted(root.glob("sha256/*/*")):
        trash = _TRASH.match(path.name)
        if trash is not None:
            gone = _leftover(path, trash[1], keep, cutoff)
        else:
            gone = path.name not in keep and _collect(path, cutoff)
        if gone:
            removed.append(path.name if trash is None else trash[1])
    return tuple(removed)


def _collect(path: Path, cutoff: float) -> bool:
    """One unreferenced candidate: True when it was deleted."""
    if not _old(path, cutoff):
        return False
    seam("gc:stated")
    if not SEAMS.rules:
        path.unlink()
        return True
    trash = path.parent / trash_name(path.name)
    try:
        path.rename(trash)
    except FileNotFoundError:
        return False
    seam("gc:renamed")
    if _old(trash, cutoff):
        return _unlinked(trash)
    # A put refreshed it before the rename: it is wanted again.
    link_back(trash, path)
    _unlinked(trash)
    return False


def _leftover(trash: Path, sha: str, keep: frozenset[str], cutoff: float) -> bool:
    """A trash name a gc left behind when it stopped between rename and unlink, removed once it
    is older than the grace. A referenced one is linked back first, so no named artifact is
    lost."""
    if not _old(trash, cutoff):
        return False
    content = trash.parent / sha
    if sha in keep:
        link_back(trash, content)
    return _unlinked(trash) and not content.exists()


def _old(path: Path, cutoff: float) -> bool:
    """Older than the grace; a file that vanished meanwhile is not a candidate."""
    try:
        return os.stat(path).st_mtime <= cutoff
    except FileNotFoundError:
        return False


def _unlinked(path: Path) -> bool:
    """Unlinks a trash name; False when another process got there first."""
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
