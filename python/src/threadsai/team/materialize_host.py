"""Materializing a host member (spec/schema/README.md, "Teams Phase 2"): its root thread opens
with `thread_started{host_member}` alone.

A host member has no task, so its open records no `user_input` and opens no turn; the row's
branch is set and it becomes idle, ready for the first caller's mail. A failed rebind ends it
in the same append, with no turn to close.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import JsonValue, TypeAdapter

from threadsai.log import BranchId, MemberStartedEvent, ParseError, ThreadId
from threadsai.result import Err, Ok
from threadsai.store import SqliteStore
from threadsai.store.conn import Conn
from threadsai.store.lease import Lease
from threadsai.store.lines import Draft, uuid7
from threadsai.store.opening import BranchOpening
from threadsai.team.batch import Batch
from threadsai.team.materialize_types import (
    LINE_ZERO,
    Materialized,
    MaterializeOptions,
    Rebind,
)
from threadsai.team.rows import MemberRow, member_named, team_row
from threadsai.team.settle import SettleContext, settle

_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


@dataclass(frozen=True, slots=True)
class _Start:
    row: MemberRow
    started: MemberStartedEvent
    pinned: Mapping[str, JsonValue]


async def materialize_host(
    store: SqliteStore, team: str, row: MemberRow, o: MaterializeOptions
) -> Ok[Materialized] | Err[ParseError]:
    """Opens the host member's root thread, or reports why nothing was opened."""
    found = await _start(store, team, row, o)
    if isinstance(found, Err):
        return found
    if found.value is None:
        return Ok(Materialized("not_starting"))
    s = found.value
    rebind = await o.rebind(s.started, None)
    branch = o.branch_id or BranchId(uuid7(o.clock()))

    def decide(conn: Conn, now: int) -> BranchOpening | None:
        live = member_named(conn, team, row.name)
        if live is None or live.state != "starting" or live.generation != row.generation:
            return None
        batch = Batch(0, now, o.mint)
        _first_events(conn, batch, s, rebind, branch)
        ttl = o.ttl_ms if rebind.status == "ok" else 0
        held = Lease(o.holder, 1, now + ttl)
        thread = ThreadId(s.started.data.thread_id)
        return BranchOpening("", thread, branch, held, tuple(batch.drafts))

    opened = await store.open_checked(decide, o.clock)
    if isinstance(opened, Err):
        return opened
    writer = opened.value
    if isinstance(writer, str) or writer is None:
        return Ok(Materialized("not_starting"))
    if rebind.status == "ok":
        return Ok(Materialized("materialized", writer))
    return Ok(Materialized("rebind_failed", code=rebind.status))


def _first_events(conn: Conn, batch: Batch, s: _Start, rebind: Rebind, branch: BranchId) -> None:
    """thread_started{host_member} and, after a failed rebind, the end of the member. There is
    no task and so no turn: a failed rebind writes no turn_completed."""
    started = s.started.data
    member = started.member
    host: JsonValue = {
        "team": member.team,
        "name": member.name,
        "generation": member.generation,
    }
    data: dict[str, JsonValue] = {
        **s.pinned,
        "config_hash": started.config_hash,
        "host_member": host,
    }
    batch.add(Draft("thread_started", data))
    if rebind.status == "ok":
        return
    code = rebind.status
    # A host member's start has no provenance: the end's notices belong to no request.
    ctx = SettleContext(conn, batch, started.thread_id, branch, None, _no_text)
    error: dict[str, JsonValue] = {"code": code, "message": f"rebind failed: {code}"}
    settle(ctx, {"status": "failed", "error": error})


def _no_text(_text: str) -> JsonValue:
    raise AssertionError("a failed rebind's result has no text")


async def _start(
    store: SqliteStore, team: str, row: MemberRow, o: MaterializeOptions
) -> Ok[_Start | None] | Err[ParseError]:
    """The member_started the host team log holds for this row, and its pinned line 0."""
    if row.state != "starting":
        return Ok(None)
    teams = await store.run(lambda c: team_row(c, team))
    if teams is None:
        return Ok(None)
    log = await store.read(BranchId(teams.team_log_branch_id), o.clock())
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
    config = await store.get_artifact(started.data.config_hash)
    if isinstance(config, Err):
        return config
    raw = _OBJECT.validate_python(json.loads(config.value))
    return Ok(_Start(row, started, {k: raw[k] for k in LINE_ZERO if k in raw}))
