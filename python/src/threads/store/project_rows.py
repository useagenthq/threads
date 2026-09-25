"""The historical projector (spec/schema/README.md, "Questions and remembered rules"): the
approval and question rows a log implies, from its settlement events alone. Import writes these
instead of running the live append hooks, which judge against today's clock: a grant that was
valid when recorded stays granted, and an undecided challenge or question is open (the
decision-time check or the expiry driver closes it later)."""

from collections.abc import Sequence
from dataclasses import dataclass

from threads.log import (
    ApprovalDeniedEvent,
    ApprovalGrantedEvent,
    ApprovalRequestedEvent,
    ParkedEvent,
    ToolResultEvent,
)
from threads.log.keys import principal_key
from threads.store.conn import Conn
from threads.store.verify import StoredEvent


@dataclass(frozen=True, slots=True)
class ApprovalRow:
    request: ApprovalRequestedEvent
    state: str
    decided_by: str | None = None
    decided_at: int | None = None


@dataclass(frozen=True, slots=True)
class QuestionRow:
    park: ParkedEvent
    state: str
    decided_at: int | None = None


@dataclass(frozen=True, slots=True)
class Rows:
    approvals: tuple[ApprovalRow, ...]
    questions: tuple[QuestionRow, ...]


def project_rows(events: Sequence[StoredEvent]) -> Rows:
    """Each approval_requested as granted, denied or open; each question park as answered,
    expired or open. No clock is read."""
    approvals: dict[str, ApprovalRow] = {}
    questions: dict[str, QuestionRow] = {}
    for event in events:
        match event:
            case ApprovalRequestedEvent():
                approvals[event.data.challenge_id] = ApprovalRow(event, "open")
            case ApprovalGrantedEvent() | ApprovalDeniedEvent():
                asked = approvals.get(event.data.challenge_id)
                if asked is not None:
                    state = "granted" if isinstance(event, ApprovalGrantedEvent) else "denied"
                    by = principal_key(event.actor.principal)
                    approvals[event.data.challenge_id] = ApprovalRow(
                        asked.request, state, by, event.time
                    )
            case ParkedEvent() if event.data.reason == "awaiting_input":
                questions[event.data.address.id] = QuestionRow(event, "open")
            case ToolResultEvent() if event.data.call_id in questions:
                park = questions[event.data.call_id].park
                state = "answered" if event.data.origin == "answered" else "expired"
                questions[event.data.call_id] = QuestionRow(park, state, event.time)
            case _:
                pass
    return Rows(tuple(approvals.values()), tuple(questions.values()))


def write_rows(conn: Conn, tenant_id: str, rows: Rows) -> None:
    """The projected rows, in the caller's transaction; a row already there is kept."""
    for a in rows.approvals:
        e = a.request
        conn.execute(
            "INSERT INTO approvals (challenge_id, tenant_id, installation_id, thread_id, branch_id,"
            " call_id, args_hash, file_hashes, expires_at, state, decided_by, decided_at)"
            " VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (
                e.data.challenge_id,
                tenant_id,
                e.thread_id,
                e.branch_id,
                e.data.call_id,
                e.data.args_hash,
                b"[]",
                e.data.expires_at,
                a.state,
                a.decided_by,
                a.decided_at,
            ),
        )
    for q in rows.questions:
        p = q.park
        expires = p.data.expires_at
        conn.execute(
            "INSERT INTO questions (tenant_id, branch_id, call_id, expires_at, state, decided_at)"
            " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (
                tenant_id,
                p.branch_id,
                p.data.address.id,
                expires if isinstance(expires, int) else 0,
                q.state,
                q.decided_at,
            ),
        )
