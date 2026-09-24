"""Materialize (spec/schema/README.md, "Teams"; design §4.10): the team worker's first consume of
a starting member. Its prework, outside any transaction, rebinds the member's definition by name;
then one write transaction re-reads the row (gone or no longer starting commits nothing) and opens
the member's branch with thread_started and its task as user_input. A failed rebind is one
complete end-of-member append instead, and no model request is ever made for it. Reference:
spec/tools/fixtures/ops_start.py."""

import json
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import JsonValue, TypeAdapter
from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    BranchId,
    MailEnvelope,
    MemberStartedEvent,
    OperatorSender,
    ParseError,
    ThreadId,
)
from threads.reduce.handlers import to_json
from threads.result import Err, Ok
from threads.store import SqliteStore, Writer
from threads.store.lease import Lease
from threads.store.lines import Draft, uuid7
from threads.store.opening import BranchOpening
from threads.store.worker import Clock
from threads.team.batch import Batch, Mint
from threads.team.rows import MemberRow, member_named, pending_to, team_row
from threads.team.settle import SettleContext, settle

type RebindCode = Literal["pin_unavailable", "pin_mismatch"]


@dataclass(frozen=True, slots=True)
class Rebind:
    """What rebinding the member's definition by name found: ok (with a nested lead's own team,
    which its first append opens), or why not."""

    status: Literal["ok", "pin_unavailable", "pin_mismatch"]
    team: Mapping[str, JsonValue] | None = None


@dataclass(frozen=True, slots=True)
class Materialized:
    status: Literal["materialized", "not_starting", "rebind_failed"]
    writer: Writer | None = None
    code: RebindCode | None = None


@dataclass(frozen=True, slots=True)
class MaterializeOptions:
    rebind: Callable[[MemberStartedEvent, MailEnvelope], Awaitable[Rebind]]
    """Rebinds the member's definition from its member_started and task (whose sender is the
    starter a dynamic member's block names)."""
    holder: str
    """The lease holder the member's first writer runs under."""
    ttl_ms: int
    clock: Clock
    mint: Mint | None = None
    branch_id: BranchId | None = None
    """The member's branch id; a new one by default."""


@dataclass(frozen=True, slots=True)
class _Starting:
    row: MemberRow
    task: MailEnvelope
    started: MemberStartedEvent


_LINE_ZERO: Final = (
    "agent_name",
    "instructions",
    "model",
    "model_params",
    "adapter",
    "tools",
    "policy",
    "sandbox_provider",
)
"""thread_started takes the pinned config's line-0 fields; the rest of the config is hashed only."""
_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


async def materialize(
    store: SqliteStore, team: str, name: str, o: MaterializeOptions
) -> Ok[Materialized] | Err[ParseError]:
    """Materializes the member `name` of `team`, or reports why nothing was opened."""
    found = await _starting(store, team, name, o.clock())
    if isinstance(found, Err) or found.value is None:
        return found if isinstance(found, Err) else Ok(Materialized("not_starting"))
    s = found.value
    rebind = await o.rebind(s.started, s.task)
    config = await store.get_artifact(s.started.data.config_hash)
    if isinstance(config, Err):
        return config
    raw = _OBJECT.validate_python(json.loads(config.value))
    pinned = {k: raw[k] for k in _LINE_ZERO if k in raw}
    branch = o.branch_id or BranchId(uuid7(o.clock()))

    def decide(conn: sqlite3.Connection, now: int) -> BranchOpening | None:
        row = member_named(conn, team, name)
        if row is None or row.state != "starting" or row.generation != s.row.generation:
            return None
        batch = Batch(0, now, o.mint)
        _first_events(conn, batch, s, pinned, rebind, branch)
        ttl = o.ttl_ms if rebind.status == "ok" else 0
        held = Lease(o.holder, 1, now + ttl)
        thread = ThreadId(s.started.data.thread_id)
        return BranchOpening("", thread, branch, held, tuple(batch.drafts))

    opened = await store.open_checked(decide, o.clock)
    if isinstance(opened, Err):
        return opened
    writer = opened.value
    if not isinstance(writer, Writer):
        return Ok(Materialized("not_starting"))
    if rebind.status == "ok":
        return Ok(Materialized("materialized", writer))
    return Ok(Materialized("rebind_failed", code=rebind.status))


def _first_events(  # noqa: PLR0913, PLR0917 - one opening: where, of whom, with what
    conn: sqlite3.Connection,
    batch: Batch,
    s: _Starting,
    pinned: Mapping[str, JsonValue],
    rebind: Rebind,
    branch: BranchId,
) -> None:
    """thread_started (the pinned config, the member_started's parent) and the task's user_input,
    whose actor is the task's principal; after a failed rebind, the turn closes before any model
    request and the member ends failed with everything an end carries."""
    started = s.started.data
    data: dict[str, JsonValue] = {
        **pinned,
        "config_hash": started.config_hash,
        "parent": to_json(started.parent),
    }
    if rebind.status == "ok" and rebind.team is not None:
        data["team"] = dict(rebind.team)
    batch.add(Draft("thread_started", data))
    task = s.task
    body = task.body
    if body is MISSING or body.text is MISSING:
        raise AssertionError(f"task {task.mail_id} has no text")
    text = body.text
    actor: dict[str, JsonValue] = {"kind": "host", "principal": to_json(task.provenance.principal)}
    user: dict[str, JsonValue] = {
        "source": "team_task",
        "text": text,
        "mail_id": task.mail_id,
    }
    batch.add(Draft("user_input", user, actor))
    if rebind.status == "ok":
        return
    code = rebind.status
    batch.add(Draft("turn_completed", {"reason": "error", "code": code}))
    ctx = SettleContext(conn, batch, started.thread_id, branch, to_json(task.provenance), _no_text)
    error: dict[str, JsonValue] = {"code": code, "message": f"rebind failed: {code}"}
    settle(ctx, {"status": "failed", "error": error})


def _no_text(_text: str) -> JsonValue:
    raise AssertionError("a failed rebind's result has no text")


async def _starting(
    store: SqliteStore, team: str, name: str, now: int
) -> Ok[_Starting | None] | Err[ParseError]:
    """The starting row, its pending task and its member_started, read from the starter's log."""
    row = await store.run(lambda c: member_named(c, team, name))
    if row is None or row.state != "starting":
        return Ok(None)
    pending = await store.run(lambda c: pending_to(c, team, name))
    task = next((m for m in pending if m.kind == "task"), None)
    teams = await store.run(lambda c: team_row(c, team))
    if task is None or teams is None:
        return Ok(None)
    started = await started_by(store, (teams.team_log_branch_id, task), row, now)
    return started if isinstance(started, Err) else Ok(_Starting(row, task, started.value))


async def started_by(
    store: SqliteStore, where: tuple[str, MailEnvelope], row: MemberRow, now: int
) -> Ok[MemberStartedEvent] | Err[ParseError]:
    """A member's member_started, read from its starter's log: the team log (`where`'s branch)
    for an operator start, else the log the task (`where`'s envelope) was sent from."""
    team_log, task = where
    if isinstance(task.from_, OperatorSender):
        starter: Ok[BranchId] | Err[ParseError] = Ok(BranchId(team_log))
    else:
        starter = await store.root(ThreadId(task.causal.thread_id))
    if isinstance(starter, Err):
        return starter
    log = await store.read(starter.value, now)
    if isinstance(log, Err):
        return log
    started = next(
        (
            e
            for e in log.value.fold.events
            if isinstance(e, MemberStartedEvent)
            and e.data.member.name == row.name
            and e.data.member.generation == row.generation
        ),
        None,
    )
    if started is None:
        return Err(ParseError("log_corrupt", f"no member_started for {row.name}"))
    return Ok(started)
