"""Synchronous SQLite statements of the log store. Every function runs on the store's
single worker thread, and every write is one `BEGIN IMMEDIATE` transaction.

Rows hold each line's exact bytes. Reading a branch back always goes through `verify_export`,
so a row is never trusted just because it is in the database.
"""

import sqlite3
import time
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from itertools import pairwise
from typing import Final

from threads._generated.store_sql import STORE_SQL, STORE_VERSION
from threads.log import BranchId, ForkEvent, ParseError, ThreadId
from threads.log.digest import sha256_hex
from threads.store.lines import head_line
from threads.store.project_rows import project_rows, write_rows
from threads.store.verify import Segment, StoredEvent, VerifiedLog

LOCAL_TENANT: Final = "local"
"""The tenant of local use: the local operator's (spec/api.json)."""


@dataclass(frozen=True, slots=True)
class Branch:
    branch_id: BranchId
    thread_id: ThreadId
    tenant_id: str
    parent: BranchId | None
    fork_at_seq: int | None
    header_line: bytes
    state: str
    head_seq: int
    head_hash: str
    head_verified: bool = True
    """False after importing a log with no head checkpoint or a torn tail."""
    dropped_ref: str | None = None
    """The sha256 of the torn tail's artifact, if the import dropped bytes."""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    _wal(conn)
    # Durable ack: a commit returns only after a full sync. fullfsync
    # matters on darwin, where plain fsync doesn't flush the drive cache; elsewhere it's a no-op.
    for pragma in ("synchronous=FULL", "fullfsync=ON"):
        conn.execute(f"PRAGMA {pragma}")
    conn.execute("PRAGMA checkpoint_fullfsync=ON")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


WAL_TRIES = 500


def _wal(conn: sqlite3.Connection) -> None:
    """Switching a new file to WAL can meet another process doing the same, and SQLite answers
    that busy without calling its busy handler: retried, so two hosts can open one fresh store."""
    for tried in range(1, WAL_TRIES + 1):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as error:
            if "locked" not in str(error) or tried == WAL_TRIES:
                raise
            time.sleep(0.01)
        else:
            return


def install(conn: sqlite3.Connection) -> ParseError | None:
    """Creates the tables of spec/schema/store.sql on a new database. A database a newer schema
    wrote is refused, never downgraded; one an earlier version wrote is refused too, since
    stores are not migrated."""
    (found,) = conn.execute("PRAGMA user_version").fetchone()
    if int_of(found) > STORE_VERSION:
        message = f"store schema {found} is newer than {STORE_VERSION}"
        return ParseError("unsupported_format", message)
    if 0 < int_of(found) < STORE_VERSION:
        message = f"this store was created by an earlier threads version (schema {found});"
        return ParseError("unsupported_format", f"{message} create a new store")
    conn.executescript(STORE_SQL)
    return None


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def branch(conn: sqlite3.Connection, branch_id: BranchId) -> Branch | None:
    row: tuple[object, ...] | None = conn.execute(
        "SELECT thread_id, tenant_id, parent_branch_id, fork_at_seq, header_line, state, head_seq,"
        " head_hash, head_verified, dropped_ref FROM branches WHERE branch_id = ?",
        (branch_id,),
    ).fetchone()
    if row is None:
        return None
    thread, tenant, parent, at_seq, header, state, head_seq, head_hash, verified, dropped = row
    return Branch(
        branch_id,
        ThreadId(text_of(thread)),
        text_of(tenant),
        None if parent is None else BranchId(text_of(parent)),
        None if at_seq is None else int_of(at_seq),
        blob_of(header),
        text_of(state),
        int_of(head_seq),
        text_of(head_hash),
        int_of(verified) == 1,
        None if dropped is None else text_of(dropped),
    )


def export(conn: sqlite3.Connection, branch_id: BranchId) -> bytes:
    """The branch's export: each ancestor segment through its fork point, the branch's own
    lines, then the committed head checkpoint. An unverified import has no
    head line: the caller appends its dropped bytes, so the export stays unverified."""
    found = branch(conn, branch_id)
    if found is None:
        raise LookupError(f"no branch {branch_id}")
    lines = segments(conn, found, found.head_seq)
    if not found.head_verified:
        return lines
    return lines + head_line(found.branch_id, found.head_seq, found.head_hash) + b"\n"


def prefix(conn: sqlite3.Connection, branch_id: BranchId, through: int) -> bytes:
    """The resolved chain of a branch through `through`, without a head checkpoint."""
    found = branch(conn, branch_id)
    if found is None:
        raise LookupError(f"no branch {branch_id}")
    return segments(conn, found, through)


def segments(conn: sqlite3.Connection, found: Branch, through: int) -> bytes:
    """Lines of the resolved chain through `through`, one per line. Parent rows are read in
    place, never copied."""
    ancestors = b""
    if found.parent is not None and found.fork_at_seq is not None:
        parent = branch(conn, found.parent)
        if parent is None:
            raise LookupError(f"branch {found.branch_id} names a missing parent")
        ancestors = segments(conn, parent, min(through, found.fork_at_seq))
    rows: list[tuple[object]] = conn.execute(
        "SELECT line FROM events WHERE branch_id = ? AND seq <= ? ORDER BY seq",
        (found.branch_id, through),
    ).fetchall()
    own = b"".join(blob_of(line) + b"\n" for (line,) in rows)
    return ancestors + found.header_line + b"\n" + own


def root(conn: sqlite3.Connection, thread_id: ThreadId, tenant_id: str) -> BranchId | None:
    row: tuple[object] | None = conn.execute(
        "SELECT branch_id FROM branches WHERE thread_id = ? AND tenant_id = ?"
        " AND parent_branch_id IS NULL",
        (thread_id, tenant_id),
    ).fetchone()
    return None if row is None else BranchId(text_of(row[0]))


def forking(conn: sqlite3.Connection, tenant_id: str) -> tuple[BranchId, ...]:
    rows: list[tuple[object]] = conn.execute(
        "SELECT branch_id FROM branches WHERE tenant_id = ? AND state = 'forking'", (tenant_id,)
    ).fetchall()
    return tuple(BranchId(text_of(row[0])) for row in rows)


def mark_repaired(conn: sqlite3.Connection, branch_id: BranchId) -> None:
    """A torn import becomes runnable once its log_repaired is committed."""
    with transaction(conn):
        conn.execute(
            "UPDATE branches SET state = 'ready' WHERE branch_id = ? AND dropped_ref IS NOT NULL",
            (branch_id,),
        )


def insert_branch(conn: sqlite3.Connection, row: Branch) -> None:
    """Inserts the branch and, for a new thread, its thread row. The foreign key refuses a
    branch whose tenant is not its thread's."""
    conn.execute(
        "INSERT INTO threads (thread_id, tenant_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
        (row.thread_id, row.tenant_id),
    )
    conn.execute(
        "INSERT INTO branches (branch_id, thread_id, tenant_id, parent_branch_id, fork_at_seq,"
        " header_line, state, head_seq, head_hash, head_verified, dropped_ref)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row.branch_id,
            row.thread_id,
            row.tenant_id,
            row.parent,
            row.fork_at_seq,
            row.header_line,
            row.state,
            row.head_seq,
            row.head_hash,
            int(row.head_verified),
            row.dropped_ref,
        ),
    )


def insert_events(
    conn: sqlite3.Connection, events: Sequence[tuple[StoredEvent, bytes]], head_hash: str
) -> None:
    """Inserts one branch's rows and moves its head checkpoint, in the caller's transaction."""
    if not events:
        return
    conn.executemany(
        "INSERT INTO events (branch_id, seq, event_id, type, type_version, critical, epoch,"
        " line) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (e.branch_id, e.seq, e.event_id, e.type, e.type_version, e.critical, e.epoch, line)
            for e, line in events
        ],
    )
    last = events[-1][0]
    conn.execute(
        "UPDATE branches SET head_seq = ?, head_hash = ? WHERE branch_id = ?",
        (last.seq, head_hash, last.branch_id),
    )


def insert_segments(
    conn: sqlite3.Connection, log: VerifiedLog, tenant_id: str, dropped_ref: str | None
) -> ParseError | None:
    """Stores a verified export's segments, byte for byte, in the caller's transaction. A branch
    already in the store must hold the same lines (an idempotent re-import). A leaf whose head
    didn't verify keeps that evidence (and the dropped bytes' artifact) and is inspection-only
    until recovery records log_repaired."""
    new: list[Segment] = []
    for segment in log.segments:
        existing = branch(conn, segment.header.branch_id)
        if existing is None:
            new.append(segment)
        elif existing.tenant_id != tenant_id:
            return ParseError("branch_not_found", f"no branch {existing.branch_id}")
        elif not _holds(conn, existing, segment, _evidence(log, segment, dropped_ref)):
            return ParseError("seq_conflict", f"branch {existing.branch_id} has other lines")
    if new and _owner(conn, log.segments[0].header.thread_id) not in (None, tenant_id):
        # The owner stays unnamed: a tenant learns only that the thread id is taken.
        thread = log.segments[0].header.thread_id
        return ParseError("branch_exists", f"thread {thread} already exists")
    parents = {s.header.branch_id: p.header.branch_id for p, s in pairwise(log.segments)}
    for segment in new:
        row = _row(segment, tenant_id, parents.get(segment.header.branch_id))
        if segment is log.segments[-1] and not log.head_verified:
            row = replace(row, state="inspection_only", head_verified=False)
            row = replace(row, dropped_ref=dropped_ref)
        insert_branch(conn, row)
        insert_events(conn, segment.events, sha256_hex(segment.last_line))
        # The index rows its settlements imply, never judged against today's clock.
        write_rows(conn, tenant_id, project_rows([e for e, _ in segment.events]))
    return None


def import_segments(
    conn: sqlite3.Connection, log: VerifiedLog, tenant_id: str, dropped_ref: str | None
) -> ParseError | None:
    """`insert_segments` in a transaction of its own."""
    with transaction(conn):
        return insert_segments(conn, log, tenant_id, dropped_ref)


def _evidence(
    log: VerifiedLog, segment: Segment, dropped_ref: str | None
) -> tuple[bool, str | None] | None:
    """What the import says about the leaf's head: verified, and the torn bytes' artifact. An
    ancestor segment is only a prefix and says nothing."""
    return (log.head_verified, dropped_ref) if segment is log.segments[-1] else None


def _holds(
    conn: sqlite3.Connection,
    existing: Branch,
    segment: Segment,
    evidence: tuple[bool, str | None] | None,
) -> bool:
    last = segment.events[-1][0].seq if segment.events else 0
    if evidence is not None and (existing.head_seq, *evidence) != (
        last,
        existing.head_verified,
        existing.dropped_ref,
    ):
        return False
    stored = segments(conn, existing, last)
    return existing.head_seq >= last and stored.endswith(_lines(segment))


def _owner(conn: sqlite3.Connection, thread_id: ThreadId) -> str | None:
    row: tuple[object] | None = conn.execute(
        "SELECT tenant_id FROM threads WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    return None if row is None else text_of(row[0])


def _row(segment: Segment, tenant_id: str, parent: BranchId | None) -> Branch:
    first = segment.events[0][0] if segment.events else None
    repair = isinstance(first, ForkEvent) and first.data.reason == "repair"
    return Branch(
        segment.header.branch_id,
        segment.header.thread_id,
        tenant_id,
        parent,
        segment.fork_at_seq,
        segment.header_line,
        # A repair child is never runnable.
        "inspection_only" if repair else "ready",
        segment.events[-1][0].seq if segment.events else 0,
        sha256_hex(segment.last_line),
    )


def _lines(segment: Segment) -> bytes:
    return segment.header_line + b"\n" + b"".join(line + b"\n" for _, line in segment.events)


def text_of(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected TEXT, got {value!r}")
    return value


def int_of(value: object) -> int:
    if not isinstance(value, int):
        raise TypeError(f"expected INTEGER, got {value!r}")
    return value


def blob_of(value: object) -> bytes:
    if not isinstance(value, bytes):
        raise TypeError(f"expected BLOB, got {value!r}")
    return value
