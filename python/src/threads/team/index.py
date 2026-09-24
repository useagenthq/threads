"""The team index (spec/schema/README.md, "Teams", the replay rule): the rows of store.sql's team
tables, written from the events that hold every byte they need.

One module serves both paths, so the replay rule holds by construction: the writer's append calls
`insert_rows` then `change_rows` on its drafts, and a rebuild calls `insert_rows` over every log of
the team, then `change_rows` over every log. Inserts come first, so a rebuild doesn't depend on
the order the logs are read in. The reference is spec/tools/fixtures/team_index.py.
"""

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel
from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    AgentFinishedEvent,
    AgentSpawnedEvent,
    AskClosedEvent,
    BranchId,
    Event,
    MailEnvelope,
    MailRefusedEvent,
    MemberEndedEvent,
    MemberIdleEvent,
    MemberObservedEvent,
    MemberRef,
    MemberStartedEvent,
    MessageReceivedEvent,
    MessageSentEvent,
    MonitorSetEvent,
    OperatorRequestEvent,
    ParkedEvent,
    ResumedEvent,
    TeamOpenedEvent,
    ThreadId,
    ThreadStartedEvent,
    UserInputEvent,
    WaitFinishedEvent,
    WaitStartedEvent,
)
from threads.log.jcs import canonicalize
from threads.log.keys import principal_key
from threads.reduce import Fold, apply, enter_segment
from threads.reduce.handlers import to_json
from threads.result import Err
from threads.store.verify import VerifiedLog

_SCOPED: Final = "(? IS NULL OR team_id = ?)"
"""A rebuild of one team writes only that team's rows (a nested lead's log also names its own)."""
_STATES: Final[Mapping[type, str]] = {
    ParkedEvent: "parked",
    ResumedEvent: "running",
    MemberIdleEvent: "idle",
    MemberEndedEvent: "ended",
}
"""A member's own events that set its row's state; a turn opener sets running."""


@dataclass(frozen=True, slots=True)
class TeamLog:
    """The log an index write reads from: its thread and branch."""

    thread_id: ThreadId
    branch_id: BranchId


def json_bytes(model: BaseModel) -> bytes:
    """A JSON column: the RFC 8785 bytes of the parsed value, as the log line holds them."""
    text = canonicalize(to_json(model))
    if isinstance(text, Err):
        raise ValueError(f"a stored value is not canonical JSON: {text.error}")
    return text.value.encode()


def turn_openers(log: VerifiedLog) -> frozenset[str]:
    """The event ids that opened a turn, as the reducer decides it: re-folded with its own apply,
    so turn opening is never decided twice."""
    fold, opened = Fold(now=0), set[str]()
    for segment in log.segments:
        enter_segment(fold, segment.header)
        for event, _ in segment.events:
            before = fold.in_turn
            if apply(fold, event) is None and fold.in_turn and not before:
                opened.add(event.event_id)
    return frozenset(opened)


# ---------- pass 1: rows a sender's or starter's append inserts ----------


def insert_rows(
    conn: sqlite3.Connection, log: TeamLog, events: Sequence[Event], scope: str | None = None
) -> None:
    for event in events:
        insert = _INSERTS.get(type(event))
        if insert is not None:
            insert(_Write(conn, log, scope), event)


@dataclass(frozen=True, slots=True)
class _Write:
    conn: sqlite3.Connection
    log: TeamLog
    scope: str | None

    def ours(self, team: str) -> bool:
        return self.scope is None or self.scope == team

    def insert(self, table: str, row: Mapping[str, object]) -> None:
        names = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self.conn.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", tuple(row.values()))  # noqa: S608

    def scoped(self, sql: str, *params: object) -> None:
        """An UPDATE or DELETE whose WHERE ends in the team scope."""
        self.conn.execute(f"{sql} AND {_SCOPED}", (*params, self.scope, self.scope))


type _Step = Callable[[_Write, Event], None]


def _on[E](cls: type[E], step: Callable[[_Write, E], None]) -> tuple[type, _Step]:
    """Binds a step typed for one event class into the table's uniform signature."""

    def run(w: _Write, event: Event) -> None:
        if not isinstance(event, cls):
            raise TypeError(f"{type(event).__name__} dispatched to the {cls.__name__} step")
        step(w, event)

    return cls, run


def _opened(w: _Write, e: TeamOpenedEvent) -> None:
    d = e.data
    if w.ours(d.team):
        row = {"team_id": d.team, "tenant_id": d.lead.tenant, "lead_thread_id": d.lead_thread_id}
        w.insert("teams", {**row, "team_log_branch_id": w.log.branch_id, "closed_at": None})


def _lead(w: _Write, e: ThreadStartedEvent) -> None:
    d = e.data
    if d.team is MISSING or not w.ours(d.team.id):
        return
    key = {"team_id": d.team.id, "name": d.agent_name, "generation": 1, "role": "lead"}
    w.insert(
        "team_members",
        {**key, "agent": d.agent_name, "config_hash": d.config_hash}
        | {"thread_id": w.log.thread_id, "branch_id": w.log.branch_id, "provenance": None}
        | {"state": "running", "result": None, "updated_seq": e.seq},
    )


def _started(w: _Write, e: MemberStartedEvent) -> None:
    d, m = e.data, e.data.member
    if not w.ours(m.team):
        return
    key = {"team_id": m.team, "name": m.name, "generation": m.generation, "role": "member"}
    w.insert(
        "team_members",
        {**key, "agent": d.agent, "config_hash": d.config_hash}
        | {"thread_id": d.thread_id, "branch_id": None, "provenance": json_bytes(d.provenance)}
        | {"state": "starting", "result": None, "updated_seq": e.seq},
    )
    _monitor(w, f"{w.log.branch_id}:{e.event_id}:task", m, "task")


def _monitor(w: _Write, monitor_id: str, m: MemberRef, kind: str, wait: str | None = None) -> None:
    if w.ours(m.team):
        row = {"monitor_id": monitor_id, "team_id": m.team, "watcher_branch_id": w.log.branch_id}
        target = {"target_name": m.name, "target_generation": m.generation}
        w.insert("monitors", {**row, **target, "kind": kind, "wait_id": wait})


def _waited(w: _Write, e: WaitStartedEvent) -> None:
    for m in e.data.members:
        _monitor(w, f"{w.log.branch_id}:{e.event_id}:{m.name}", m, "settle", e.data.wait_id)


def _watched(w: _Write, e: MonitorSetEvent) -> None:
    m = e.data.member
    _monitor(w, f"{w.log.branch_id}:{e.event_id}:{m.name}", m, "end")


def _receipt(w: _Write, e: OperatorRequestEvent) -> None:
    d = e.data
    if d.idempotency_key is MISSING:
        return
    found: tuple[str, str] | None = w.conn.execute(
        "SELECT tenant_id, team_id FROM teams WHERE team_log_branch_id = ?", (w.log.branch_id,)
    ).fetchone()
    if found is None or not w.ours(found[1]):
        return
    key = {"tenant_id": found[0], "team_id": found[1], "op": d.op}
    w.insert(
        "operator_receipts",
        {**key, "idempotency_key": d.idempotency_key, "principal_key": principal_key(d.principal)}
        | {"body_hash": d.body_hash, "request_id": d.request_id},
    )


def _mail(w: _Write, e: MessageSentEvent) -> None:
    env = e.data.envelope
    if not w.ours(env.team):
        return
    to = None if env.to == "team_log" else env.to
    root = env.provenance.root_request
    w.insert(
        "mail",
        {"mail_id": env.mail_id, "team_id": env.team, "kind": env.kind}
        | {"to_name": None if to is None else to.name}
        | {"to_generation": None if to is None else to.generation}
        | {"principal_key": principal_key(env.provenance.principal)}
        | {"root_request": f"{root.thread_id}:{root.event_id}", "envelope": json_bytes(env)}
        | {"created_at": e.time, "state": "pending", "consumed_seq": None},
    )
    if env.kind == "ask" and to is not None:
        _ask(w, env, to.name, to.generation)


def _ask(w: _Write, env: MailEnvelope, name: str, generation: int) -> None:
    row = {"ask_id": env.ask_id, "team_id": env.team, "asker_branch_id": w.log.branch_id}
    recipient = {"recipient_name": name, "recipient_generation": generation}
    w.insert("asks", {**row, **recipient, "deadline": env.deadline, "state": "open"})


def _spawned(w: _Write, e: AgentSpawnedEvent) -> None:
    if e.data.mode == "background":
        row = {"branch_id": w.log.branch_id, "child_thread_id": e.data.child_thread_id}
        w.insert("pending_wakes", row)


_INSERTS: Final[Mapping[type, _Step]] = dict(
    [
        _on(TeamOpenedEvent, _opened),
        _on(ThreadStartedEvent, _lead),
        _on(MemberStartedEvent, _started),
        _on(MessageSentEvent, _mail),
        _on(OperatorRequestEvent, _receipt),
        _on(WaitStartedEvent, _waited),
        _on(MonitorSetEvent, _watched),
        _on(AgentSpawnedEvent, _spawned),
    ]
)


# ---------- pass 2: what each recipient and member changes in its own log ----------


def change_rows(
    conn: sqlite3.Connection,
    log: TeamLog,
    events: Sequence[Event],
    opened: frozenset[str],
    scope: str | None = None,
) -> None:
    w = _Write(conn, log, scope)
    for event in events:
        move = _MOVES.get(type(event))
        if move is not None:
            move(w, event)
        _own_row(w, event, opened)


def _own_row(w: _Write, e: Event, opened: frozenset[str]) -> None:
    """The log's own team_members rows (a lead's, a member's, or both for a nested lead)."""
    me = w.log.thread_id
    state = "running" if e.event_id in opened else _STATES.get(type(e))
    if isinstance(e, ThreadStartedEvent) and e.data.parent is not MISSING:
        sql = "UPDATE team_members SET branch_id = ?, state = 'running', updated_seq = ?"
        w.scoped(sql + " WHERE thread_id = ?", w.log.branch_id, e.seq, me)
    elif state is not None:
        sql = "UPDATE team_members SET state = ?, updated_seq = ? WHERE thread_id = ?"
        w.scoped(sql, state, e.seq, me)
    if isinstance(e, MemberIdleEvent | MemberEndedEvent):
        sql = "UPDATE team_members SET result = ? WHERE thread_id = ?"
        w.scoped(sql, json_bytes(e.data.result), me)
    if isinstance(e, MemberEndedEvent):
        sql = "UPDATE teams SET closed_at = ? WHERE team_id IN"
        lead = " (SELECT team_id FROM team_members WHERE thread_id = ? AND role = 'lead')"
        w.scoped(sql + lead, e.time, me)


def _consumed(w: _Write, e: MessageReceivedEvent | UserInputEvent) -> None:
    mail_id = e.data.mail_id
    if mail_id is not MISSING:
        sql = "UPDATE mail SET state = 'consumed', consumed_seq = ? WHERE mail_id = ?"
        w.scoped(sql, e.seq, mail_id)


def _refused(w: _Write, e: MailRefusedEvent) -> None:
    gone = "stale" if e.data.code == "stale_member" else "returned"
    sql = "UPDATE mail SET state = ?, consumed_seq = ? WHERE mail_id = ?"
    w.scoped(sql, gone, e.seq, e.data.mail_id)


def _closed(w: _Write, e: AskClosedEvent) -> None:
    sql = "UPDATE asks SET state = ?, closed_seq = ? WHERE ask_id = ?"
    w.scoped(sql, e.data.outcome.status, e.seq, e.data.ask_id)


def _fired(w: _Write, e: MessageSentEvent) -> None:
    env = e.data.envelope
    if env.monitor_id is not MISSING and env.kind != "member_parked":
        w.scoped("DELETE FROM monitors WHERE monitor_id = ?", env.monitor_id)


def _observed(w: _Write, e: MemberObservedEvent) -> None:
    w.scoped("DELETE FROM monitors WHERE monitor_id = ?", e.data.monitor_id)


def _finished_wait(w: _Write, e: WaitFinishedEvent) -> None:
    w.scoped("DELETE FROM monitors WHERE wait_id = ?", e.data.wait_id)


def _woke(w: _Write, e: AgentFinishedEvent | ParkedEvent) -> None:
    """A background child's pending_wakes row goes with its agent_finished, or with the parent's
    park on it."""
    if isinstance(e, AgentFinishedEvent):
        child: str = e.data.child_thread_id
    elif e.data.address.kind == "child":
        child = e.data.address.id
    else:
        return
    sql = "DELETE FROM pending_wakes WHERE branch_id = ? AND child_thread_id = ?"
    w.conn.execute(sql, (w.log.branch_id, child))


_MOVES: Final[Mapping[type, _Step]] = dict(
    [
        _on(MessageReceivedEvent, _consumed),
        _on(UserInputEvent, _consumed),
        _on(MailRefusedEvent, _refused),
        _on(AskClosedEvent, _closed),
        _on(MessageSentEvent, _fired),
        _on(MemberObservedEvent, _observed),
        _on(WaitFinishedEvent, _finished_wait),
        _on(AgentFinishedEvent, _woke),
        _on(ParkedEvent, _woke),
    ]
)
