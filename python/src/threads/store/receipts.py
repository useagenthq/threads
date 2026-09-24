"""POST /v1/runs idempotency receipts (store.sql `run_receipts`).

The key is unique per tenant and operation. The receipt is inserted in the transaction that
appends the run's user_input, so a lost response replays it and a crash leaves neither.
"""

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import StrictStr, TypeAdapter, ValidationError
from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.events_v1 import Uuid
from threads.log import (
    BranchId,
    Event,
    EventId,
    ParseError,
    TextPart,
    ThreadId,
    ThreadStartedEvent,
    UserInputEvent,
)
from threads.log.digest import canonical_sha256
from threads.log.keys import principal_key
from threads.result import Ok
from threads.store.companion import Companion
from threads.store.sql import text_of
from threads.store.verify import StoredEvent

_log = logging.getLogger(__name__)
_FIELDS = ("tenant_id", "thread_id", "branch_id")
"""The selected row's columns, by position: a failing field is logged by name."""
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
            bad = ", ".join(_FIELDS[int(e["loc"][0])] for e in error.errors())
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


UI: Final = "ui"
"""A UI route's run, keyed `<thread_id>:<client message id>` (spec/schema/ui/README.md)."""


def ui_key(thread_id: str, message_id: str) -> str:
    return f"{thread_id}:{message_id}"


def ui_body_hash(agent: str, text: str) -> str:
    """A `ui` receipt's body hash: the run's input only (the pinned agent name, the text, the
    source), so a retry that changes any other client field still matches, and an import can
    rebuild it from the log."""
    body = canonical_sha256({"agent": agent, "input": text, "source": "api"})
    if not isinstance(body, Ok):
        raise AssertionError("an agent name and a text always canonicalize")
    return body.value


def ui_messages(conn: sqlite3.Connection, tenant_id: str, thread_id: str) -> dict[str, str]:
    """A thread's `ui` receipts: each run's client message id by run id. Read by the byte range
    of `<thread_id>:` keys (`;` is the byte after `:`), never LIKE, so the index serves it."""
    rows: list[tuple[object, object]] = conn.execute(
        "SELECT idempotency_key, run_id FROM run_receipts WHERE tenant_id = ? AND operation = ?"
        " AND idempotency_key >= ? AND idempotency_key < ?",
        (tenant_id, UI, f"{thread_id}:", f"{thread_id};"),
    ).fetchall()
    prefix = len(f"{thread_id}:")
    return {text_of(run): text_of(key)[prefix:] for key, run in rows}


def rebuild_ui(conn: sqlite3.Connection, tenant_id: str, events: Sequence[Event], now: int) -> None:
    """An import's `ui` receipts: one per user_input that carries client_message_id, keyed and
    hashed as a UI route writes it, so a retry after an import finds its run."""
    started = next((e for e in events if isinstance(e, ThreadStartedEvent)), None)
    if started is None:
        return
    for e in events:
        if not isinstance(e, UserInputEvent) or e.data.client_message_id is MISSING:
            continue
        conn.execute(
            "INSERT INTO run_receipts (tenant_id, operation, idempotency_key, principal_key,"
            " body_hash, thread_id, branch_id, run_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (
                tenant_id,
                UI,
                ui_key(e.thread_id, e.data.client_message_id),
                principal_key(e.actor.principal),
                ui_body_hash(started.data.agent_name, input_text(e)),
                e.thread_id,
                e.branch_id,
                e.event_id,
                now,
            ),
        )


def input_text(e: UserInputEvent) -> str:
    """The text a user_input carries: its text, or its text parts joined."""
    if e.data.text is not MISSING:
        return e.data.text
    parts = () if e.data.content is MISSING else e.data.content
    return "".join(p.text for p in parts if isinstance(p, TextPart))
