"""POST /v1/runs idempotency receipts (store.sql `run_receipts`).

The key is unique per tenant and operation. The receipt is inserted in the transaction that
appends the run's user_input, so a lost response replays it and a crash leaves neither.
"""

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import StrictStr, TypeAdapter, ValidationError

from threads._generated.events_v1 import Uuid
from threads.log import BranchId, EventId, ParseError, ThreadId, UserInputEvent
from threads.store.companion import Companion
from threads.store.sql import text_of
from threads.store.verify import StoredEvent

_log = logging.getLogger(__name__)
_ROW: TypeAdapter[tuple[StrictStr, Uuid, Uuid]] = TypeAdapter(tuple[StrictStr, Uuid, Uuid])


@dataclass(frozen=True, slots=True)
class Key:
    """What an Idempotency-Key is bound to."""

    tenant_id: str
    operation: str
    idempotency_key: str
    principal_key: str
    body_hash: str


@dataclass(frozen=True, slots=True)
class Receipt:
    principal_key: str
    body_hash: str
    thread_id: ThreadId
    branch_id: BranchId
    run_id: EventId


def find(conn: sqlite3.Connection, key: Key) -> Receipt | None:
    row: tuple[object, ...] | None = conn.execute(
        "SELECT principal_key, body_hash, thread_id, branch_id, run_id FROM run_receipts"
        " WHERE tenant_id = ? AND operation = ? AND idempotency_key = ?",
        (key.tenant_id, key.operation, key.idempotency_key),
    ).fetchone()
    if row is None:
        return None
    principal, body, thread, branch, run = (text_of(v) for v in row)
    return Receipt(principal, body, ThreadId(thread), BranchId(branch), EventId(run))


def unfinished(conn: sqlite3.Connection) -> tuple[tuple[str, ThreadId, BranchId], ...]:
    """(tenant, thread, branch) of every tenant's API run branches, except those whose last event
    is a turn_completed: where a crash may have left a run's turn open. The fold decides; this
    only skips branches that are certainly closed, so a restart doesn't read every API thread's
    log."""
    rows: list[tuple[object, object, object]] = conn.execute(
        "SELECT DISTINCT r.tenant_id, r.thread_id, r.branch_id FROM run_receipts r"
        " JOIN branches b ON b.branch_id = r.branch_id AND b.tenant_id = r.tenant_id"
        " JOIN events e ON e.branch_id = b.branch_id AND e.seq = b.head_seq"
        " WHERE e.type <> 'turn_completed'"
    ).fetchall()
    found: list[tuple[str, ThreadId, BranchId]] = []
    for row in rows:
        # Storage is a boundary: a row that fails its schema is skipped and said, never the
        # reason the valid ones aren't recovered.
        try:
            tenant, thread, branch = _ROW.validate_python(row)
        except ValidationError as error:
            # Only the branch, bounded, and the fields that failed: the row's text is untrusted.
            bad = ", ".join(str(e["loc"][0]) for e in error.errors())
            _log.warning(
                "threads store: run_receipts row skipped (branch %.64r; bad field %s)", row[2], bad
            )
            continue
        found.append((tenant, ThreadId(thread), BranchId(branch)))
    return tuple(found)


def insert(key: Key, now: int) -> Companion:
    """The receipt of the append's user_input. A key another request took first refuses the
    append: the caller reads that receipt and answers from it."""

    def put(conn: sqlite3.Connection, events: Sequence[StoredEvent]) -> ParseError | None:
        run = next(e for e in events if isinstance(e, UserInputEvent))
        done = conn.execute(
            "INSERT INTO run_receipts (tenant_id, operation, idempotency_key, principal_key,"
            " body_hash, thread_id, branch_id, run_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (
                key.tenant_id,
                key.operation,
                key.idempotency_key,
                key.principal_key,
                key.body_hash,
                run.thread_id,
                run.branch_id,
                run.event_id,
                now,
            ),
        )
        if done.rowcount == 1:
            return None
        return ParseError("idempotency_key_reused", "the key was taken by another request")

    return put
