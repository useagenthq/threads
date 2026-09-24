"""Retention: the artifact sweep, never automatic (deletion is store/deletion.py). The sweep
keeps every artifact any stored line or branch row names.
"""

import re
import sqlite3
import time
from pathlib import Path
from typing import Final

from threads.store.sql import blob_of, text_of

GRACE_S: Final = 7 * 24 * 3600
"""An unreferenced artifact younger than this may belong to an append in flight: kept."""
_SHA256: Final = re.compile(rb"[0-9a-f]{64}")
_PER_BRANCH: Final = ("events", "leases", "observer_cursors")


def referenced(conn: sqlite3.Connection) -> frozenset[str]:
    """Every sha256 any stored line or branch row names: the mark of the sweep. A hash-shaped
    string that is not an artifact only keeps a file that doesn't exist."""
    found: set[str] = set()
    for (line,) in conn.execute("SELECT line FROM events"):
        found.update(m.decode() for m in _SHA256.findall(blob_of(line)))
    for header, dropped in conn.execute("SELECT header_line, dropped_ref FROM branches"):
        found.update(m.decode() for m in _SHA256.findall(blob_of(header)))
        if dropped is not None:
            found.add(text_of(dropped))
    return frozenset(found)


def sweep(root: Path, keep: frozenset[str], grace_s: float = GRACE_S) -> tuple[str, ...]:
    """Removes unreferenced artifacts older than the grace period; returns their hashes."""
    removed: list[str] = []
    cutoff = time.time() - grace_s
    for path in sorted(root.glob("sha256/*/*")):
        if path.name in keep or path.stat().st_mtime > cutoff:
            continue
        path.unlink()
        removed.append(path.name)
    return tuple(removed)
