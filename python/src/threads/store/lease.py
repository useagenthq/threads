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


@dataclass(frozen=True, slots=True)
class Owner:
    """A branch and the lease that fences what is done on its behalf: appends, and the
    resource-ledger rows it creates or releases."""

    branch_id: BranchId
    lease: Lease


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


def release(conn: sqlite3.Connection, branch_id: BranchId, mine: Lease, now: int) -> None:
    """Hands a live lease back at once, only while this holder and epoch still hold it: a stale
    holder never clears a newer owner's lease. Only the lease changes; in-doubt work stays in the
    log for recovery."""
    with transaction(conn):
        if not _stale(conn, branch_id, mine, now):
            _put(conn, branch_id, Lease(mine.holder_id, mine.epoch, now))


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


def create(conn: sqlite3.Connection, row: Branch, lease: Lease | None) -> ParseError | None:
    """Inserts a new branch and, when given, its first lease, in one transaction. A child
    starts `forking` with the lease that fences its fork: it is
    neither listed nor runnable until `finish_fork`."""
    with transaction(conn):
        if branch(conn, row.branch_id) is not None:
            return ParseError("seq_conflict", f"branch {row.branch_id} already exists")
        insert_branch(conn, row)
        if lease is not None:
            _put(conn, row.branch_id, lease)
    return None


def finish_fork(
    conn: sqlite3.Connection,
    row: Branch,
    fork: tuple[StoredEvent, bytes],
    mine: Lease,
) -> ParseError | None:
    """Step 4: the child's fork event and its final state in one transaction, only while the
    fork still holds the child's lease and the branch is still forking. A child that is not
    runnable (a repair) keeps no lease."""
    with transaction(conn):
        if _stale(conn, row.branch_id, mine, fork[0].time):
            return ParseError("stale_epoch", f"epoch {mine.epoch} no longer holds the lease")
        stored = branch(conn, row.branch_id)
        if stored is None or stored.state != "forking":
            return ParseError("seq_conflict", f"branch {row.branch_id} is not forking")
        insert_events(conn, (fork,), row.head_hash)
        conn.execute(
            "UPDATE branches SET state = ? WHERE branch_id = ?", (row.state, row.branch_id)
        )
        if row.state != "ready":
            conn.execute("DELETE FROM leases WHERE branch_id = ?", (row.branch_id,))
    return None


def fail_fork(conn: sqlite3.Connection, branch_id: BranchId) -> None:
    """A fork that can't finish: its forking row becomes `fork_failed` and is never listed."""
    with transaction(conn):
        conn.execute(
            "UPDATE branches SET state = 'fork_failed' WHERE branch_id = ? AND state = 'forking'",
            (branch_id,),
        )
        conn.execute("DELETE FROM leases WHERE branch_id = ?", (branch_id,))
