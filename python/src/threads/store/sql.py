"""Synchronous SQLite statements of the log store. Every function runs on the store's
single worker thread, and every write is one `BEGIN IMMEDIATE` transaction.

Rows hold each line's exact bytes. Reading a branch back always goes through `verify_export`,
so a row is never trusted just because it is in the database.
"""

import sqlite3
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import pairwise
from typing import Final

from threads._generated.store_sql import STORE_SQL, STORE_VERSION
from threads.log import BranchId, ForkEvent, ParseError, ThreadId
from threads.log.digest import sha256_hex
from threads.store.lines import head_line
from threads.store.verify import Segment, StoredEvent, VerifiedLog

LOCAL_TENANT: Final = "local"
"""The tenant of local use: the local operator's (spec/api.json, )."""


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


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    # Durable ack: a commit returns only after a full sync. fullfsync
    # matters on darwin, where plain fsync doesn't flush the drive cache; elsewhere it's a no-op.
    for pragma in ("journal_mode=WAL", "synchronous=FULL", "fullfsync=ON"):
        conn.execute(f"PRAGMA {pragma}")
    conn.execute("PRAGMA checkpoint_fullfsync=ON")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def install(conn: sqlite3.Connection) -> ParseError | None:
    """Creates the tables of spec/schema/store.sql. A database a newer schema wrote is
    refused, never downgraded."""
    (found,) = conn.execute("PRAGMA user_version").fetchone()
    if _int(found) > STORE_VERSION:
        message = f"store schema {found} is newer than {STORE_VERSION}"
        return ParseError("unsupported_format", message)
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
        " head_hash FROM branches WHERE branch_id = ?",
        (branch_id,),
    ).fetchone()
    if row is None:
        return None
    thread, tenant, parent, at_seq, header, state, head_seq, head_hash = row
    return Branch(
        branch_id,
        ThreadId(_text(thread)),
        _text(tenant),
        None if parent is None else BranchId(_text(parent)),
        None if at_seq is None else _int(at_seq),
        _blob(header),
        _text(state),
        _int(head_seq),
        _text(head_hash),
    )


def export(conn: sqlite3.Connection, branch_id: BranchId) -> bytes:
    """The branch's export: each ancestor segment through its fork point, the branch's own
    lines, then the committed head checkpoint."""
    found = branch(conn, branch_id)
    if found is None:
        raise LookupError(f"no branch {branch_id}")
    head = head_line(found.branch_id, found.head_seq, found.head_hash)
    return segments(conn, found, found.head_seq) + head + b"\n"


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
    own = b"".join(_blob(line) + b"\n" for (line,) in rows)
    return ancestors + found.header_line + b"\n" + own


def insert_branch(conn: sqlite3.Connection, row: Branch) -> None:
    """Inserts the branch and, for a new thread, its thread row. The foreign key refuses a
    branch whose tenant is not its thread's."""
    conn.execute(
        "INSERT INTO threads (thread_id, tenant_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
        (row.thread_id, row.tenant_id),
    )
    conn.execute(
        "INSERT INTO branches (branch_id, thread_id, tenant_id, parent_branch_id, fork_at_seq,"
        " header_line, state, head_seq, head_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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


def import_segments(
    conn: sqlite3.Connection, log: VerifiedLog, tenant_id: str
) -> ParseError | None:
    """Stores a verified export's segments, byte for byte, in one transaction. A branch already
    in the store must hold the same lines (an idempotent re-import)."""
    with transaction(conn):
        new: list[Segment] = []
        for segment in log.segments:
            existing = branch(conn, segment.header.branch_id)
            if existing is None:
                new.append(segment)
            elif not _holds(conn, existing, segment):
                return ParseError("seq_conflict", f"branch {existing.branch_id} has other lines")
        parents = {s.header.branch_id: p.header.branch_id for p, s in pairwise(log.segments)}
        for segment in new:
            insert_branch(conn, _row(segment, tenant_id, parents.get(segment.header.branch_id)))
            insert_events(conn, segment.events, sha256_hex(segment.last_line))
    return None


def _holds(conn: sqlite3.Connection, existing: Branch, segment: Segment) -> bool:
    last = segment.events[-1][0].seq if segment.events else 0
    stored = segments(conn, existing, last)
    return existing.head_seq >= last and stored.endswith(_lines(segment))


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


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected TEXT, got {value!r}")
    return value


def _int(value: object) -> int:
    if not isinstance(value, int):
        raise TypeError(f"expected INTEGER, got {value!r}")
    return value


def _blob(value: object) -> bytes:
    if not isinstance(value, bytes):
        raise TypeError(f"expected BLOB, got {value!r}")
    return value
