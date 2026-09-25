"""Synchronous statements of the log store, in the portable subset (`threads.store.conn`).
Every function runs on the store's single worker thread, inside the caller's transaction.

Rows hold each line's exact bytes. Reading a branch back always goes through `verify_export`,
so a row is never trusted just because it is in the database.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from itertools import pairwise
from typing import Final

from threads.log import BranchId, ForkEvent, ParseError, ThreadId
from threads.log.digest import sha256_hex
from threads.store.conn import Conn, transaction
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


def branch(conn: Conn, branch_id: BranchId) -> Branch | None:
    row = conn.execute(
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


def export(conn: Conn, branch_id: BranchId) -> bytes:
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


def prefix(conn: Conn, branch_id: BranchId, through: int) -> bytes:
    """The resolved chain of a branch through `through`, without a head checkpoint."""
    found = branch(conn, branch_id)
    if found is None:
        raise LookupError(f"no branch {branch_id}")
    return segments(conn, found, through)


def segments(conn: Conn, found: Branch, through: int) -> bytes:
    """Lines of the resolved chain through `through`, one per line. Parent rows are read in
    place, never copied."""
    ancestors = b""
    if found.parent is not None and found.fork_at_seq is not None:
        parent = branch(conn, found.parent)
        if parent is None:
            raise LookupError(f"branch {found.branch_id} names a missing parent")
        ancestors = segments(conn, parent, min(through, found.fork_at_seq))
    rows = conn.execute(
        "SELECT line FROM events WHERE branch_id = ? AND seq <= ? ORDER BY seq",
        (found.branch_id, through),
    ).fetchall()
    own = b"".join(blob_of(line) + b"\n" for (line,) in rows)
    return ancestors + found.header_line + b"\n" + own


def root(conn: Conn, thread_id: ThreadId, tenant_id: str) -> BranchId | None:
    row = conn.execute(
        "SELECT branch_id FROM branches WHERE thread_id = ? AND tenant_id = ?"
        " AND parent_branch_id IS NULL ORDER BY rowid LIMIT 1",
        (thread_id, tenant_id),
    ).fetchone()
    return None if row is None else BranchId(text_of(row[0]))


def forking(conn: Conn, tenant_id: str) -> tuple[BranchId, ...]:
    rows = conn.execute(
        "SELECT branch_id FROM branches WHERE tenant_id = ? AND state = 'forking'", (tenant_id,)
    ).fetchall()
    return tuple(BranchId(text_of(row[0])) for row in rows)


def mark_repaired(conn: Conn, branch_id: BranchId) -> None:
    """A torn import becomes runnable once its log_repaired is committed."""
    with transaction(conn):
        conn.execute(
            "UPDATE branches SET state = 'ready' WHERE branch_id = ? AND dropped_ref IS NOT NULL",
            (branch_id,),
        )


def insert_branch(conn: Conn, row: Branch) -> bool:
    """Inserts the branch and, for a new thread, its thread row. The foreign key refuses a
    branch whose tenant is not its thread's. False when the branch is a root of a thread that
    has one (store.sql branches_root): nothing was inserted."""
    conn.execute(
        "INSERT INTO threads (thread_id, tenant_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
        (row.thread_id, row.tenant_id),
    )
    inserted = conn.execute(
        "INSERT INTO branches (branch_id, thread_id, tenant_id, parent_branch_id, fork_at_seq,"
        " header_line, state, head_seq, head_hash, head_verified, dropped_ref)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (thread_id) WHERE parent_branch_id IS NULL DO NOTHING",
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
    return inserted.rowcount == 1


def insert_events(conn: Conn, events: Sequence[tuple[StoredEvent, bytes]], head_hash: str) -> None:
    """Inserts one branch's rows and moves its head checkpoint, in the caller's transaction."""
    if not events:
        return
    conn.executemany(
        "INSERT INTO events (branch_id, seq, event_id, type, type_version, critical, epoch,"
        " line) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (e.branch_id, e.seq, e.event_id, e.type, e.type_version, int(e.critical), e.epoch, line)
            for e, line in events
        ],
    )
    last = events[-1][0]
    conn.execute(
        "UPDATE branches SET head_seq = ?, head_hash = ? WHERE branch_id = ?",
        (last.seq, head_hash, last.branch_id),
    )


def insert_segments(
    conn: Conn, log: VerifiedLog, tenant_id: str, dropped_ref: str | None
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
        if not insert_branch(conn, row):
            thread = row.thread_id
            return ParseError("branch_exists", f"thread {thread} already exists")
        insert_events(conn, segment.events, sha256_hex(segment.last_line))
        # The index rows its settlements imply, never judged against today's clock.
        write_rows(conn, tenant_id, project_rows([e for e, _ in segment.events]))
    return None


def import_segments(
    conn: Conn, log: VerifiedLog, tenant_id: str, dropped_ref: str | None
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
    conn: Conn,
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


def _owner(conn: Conn, thread_id: ThreadId) -> str | None:
    row = conn.execute("SELECT tenant_id FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
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
