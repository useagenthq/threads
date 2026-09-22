"""Single writer per branch: the lease compare-and-set and the conditional append.

Both run inside one `BEGIN IMMEDIATE` transaction, so a check and the write it guards can't be
split by another process.
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from threads.log import BranchId, ParseError
from threads.store.sql import Branch, branch, insert_branch, insert_events, transaction
from threads.store.verify import StoredEvent

TTL_MS = 30_000
"""Lease lifetime. Holders renew well before it runs out."""


@dataclass(frozen=True, slots=True)
class Lease:
    holder_id: str
    epoch: int
    expires_at: int


def _lease(conn: sqlite3.Connection, branch_id: BranchId) -> Lease | None:
    row: tuple[object, object, object] | None = conn.execute(
        "SELECT holder_id, epoch, expires_at FROM leases WHERE branch_id = ?", (branch_id,)
    ).fetchone()
    if row is None:
        return None
    holder, epoch, expires = row
    if not (isinstance(holder, str) and isinstance(epoch, int) and isinstance(expires, int)):
        raise TypeError(f"malformed lease row {row!r}")
    return Lease(holder, epoch, expires)


def _put(conn: sqlite3.Connection, branch_id: BranchId, lease: Lease) -> None:
    conn.execute(
        "INSERT INTO leases (branch_id, holder_id, epoch, expires_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (branch_id) DO UPDATE SET holder_id = excluded.holder_id,"
        " epoch = excluded.epoch, expires_at = excluded.expires_at",
        (branch_id, lease.holder_id, lease.epoch, lease.expires_at),
    )


def take(
    conn: sqlite3.Connection, branch_id: BranchId, holder_id: str, chain_epoch: int, now: int
) -> Lease | ParseError:
    """Acquires the lease if it is free or expired. The epoch is max(lease epoch, max epoch in
    the resolved chain) + 1, so a child continues its parent's epochs (wire rule 11)."""
    expires_at = now + TTL_MS
    with transaction(conn):
        current = _lease(conn, branch_id)
        if current is not None and current.holder_id != holder_id and current.expires_at > now:
            return ParseError("branch_busy", f"branch {branch_id} is leased to another writer")
        epoch = max(0 if current is None else current.epoch, chain_epoch) + 1
        lease = Lease(holder_id, epoch, expires_at)
        _put(conn, branch_id, lease)
    return lease


def _stale(conn: sqlite3.Connection, branch_id: BranchId, mine: Lease, now: int) -> bool:
    current = _lease(conn, branch_id)
    if current is None or current.expires_at <= now:
        return True
    return (current.holder_id, current.epoch) != (mine.holder_id, mine.epoch)


def check(
    conn: sqlite3.Connection, branch_id: BranchId, mine: Lease, now: int
) -> ParseError | None:
    """The gateway fence: the lease is still ours, live, at our epoch."""
    if _stale(conn, branch_id, mine, now):
        return ParseError("stale_epoch", f"epoch {mine.epoch} no longer holds the lease")
    return None


def renew(
    conn: sqlite3.Connection, branch_id: BranchId, mine: Lease, now: int
) -> Lease | ParseError:
    """Extends a live lease by one TTL. A lost or expired lease is never revived: the holder
    must stop and re-acquire, which takes a new epoch."""
    with transaction(conn):
        if _stale(conn, branch_id, mine, now):
            return ParseError("stale_epoch", f"the lease at epoch {mine.epoch} is lost")
        renewed = Lease(mine.holder_id, mine.epoch, now + TTL_MS)
        _put(conn, branch_id, renewed)
    return renewed


@dataclass(frozen=True, slots=True)
class Batch:
    """Rows for one branch, the head seq they follow, and the hash of their last line."""

    expected_seq: int
    rows: Sequence[tuple[StoredEvent, bytes]]
    head_hash: str


def append(conn: sqlite3.Connection, mine: Lease, now: int, batch: Batch) -> ParseError | None:
    """The conditional append: the lease is still ours and live, and the
    committed head is where the writer expects it. Then the rows and the head move together."""
    branch_id = batch.rows[0][0].branch_id
    with transaction(conn):
        if _stale(conn, branch_id, mine, now):
            return ParseError("stale_epoch", f"epoch {mine.epoch} no longer holds the lease")
        stored = branch(conn, branch_id)
        if stored is None or stored.head_seq != batch.expected_seq:
            message = f"the committed head is not at {batch.expected_seq}"
            return ParseError("seq_conflict", message)
        insert_events(conn, batch.rows, batch.head_hash)
    return None


def create(
    conn: sqlite3.Connection,
    row: Branch,
    rows: Sequence[tuple[StoredEvent, bytes]],
    lease: Lease | None,
) -> ParseError | None:
    """Inserts a new branch with its first rows (a child's fork event) and, when it is
    runnable, its first lease, in one transaction."""
    with transaction(conn):
        if branch(conn, row.branch_id) is not None:
            return ParseError("seq_conflict", f"branch {row.branch_id} already exists")
        insert_branch(conn, row)
        insert_events(conn, rows, row.head_hash)
        if lease is not None:
            _put(conn, row.branch_id, lease)
    return None
