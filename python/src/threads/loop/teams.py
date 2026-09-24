"""A team thread in the loop (spec/schema/README.md, "Teams"): a turn end's append carries the
member's settlement; while idle its pending mail is consumed, and each turn a receipt opens is
run. A member run by the team worker stops once nothing opens a turn; the lead of an in-process
run waits for its members until its run ends ("Run completion")."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from pydantic import JsonValue

from threads.log import Event, ParkedEvent, TurnCompletedEvent, UserInputEvent
from threads.loop.drive import parked
from threads.loop.runtime import Appended, Halt, Idle, Parked, Runtime, after_barrier, lost
from threads.loop.team_runtime import TeamRuntime
from threads.reduce.run_end import RunStatus, run_end
from threads.result import Err
from threads.store import Draft
from threads.store.writer import DecideTx, Refusal
from threads.team.batch import Batch
from threads.team.constants import TEAM_CONSTANTS
from threads.team.consume import ConsumeContext, consume
from threads.team.park import park_notice
from threads.team.provenance import turn_provenance
from threads.team.settle import Completed, SettleContext, settle
from threads.team.turn_end import settlement_of


def run_status(events: Sequence[Event]) -> RunStatus:
    """The status of the latest request's run."""
    request = next((e for e in reversed(events) if isinstance(e, UserInputEvent)), None)
    return "running" if request is None else run_end(events, request.event_id).status


def _team(rt: Runtime) -> TeamRuntime:
    if rt.team is None:
        raise AssertionError("a team step outside a team")
    return rt.team


def _thread(rt: Runtime) -> str:
    thread = rt.fold.thread_id
    if thread is None:
        raise AssertionError("an acquired branch has a thread")
    return thread


async def put_text(rt: Runtime, text: str) -> JsonValue:
    """A text value stored before the append that names it (C5 checks it on the way in)."""
    data = text.encode()
    sha = await rt.store.put_artifact(data)
    return {"sha256": sha, "bytes": len(data), "media_type": "text/plain"}


async def settled(rt: Runtime, drafts: Sequence[Draft]) -> Appended:
    """The turn end, with member_idle or member_ended and what they send, in one append; a
    member's first park, with its one member_parked notice to its starter."""
    team = _team(rt)
    events = rt.events
    ends = [i for i, e in enumerate(events) if isinstance(e, TurnCompletedEvent)]
    how = settlement_of(events[ends[-1] + 1 if ends else 0 :], drafts)
    big: JsonValue = None
    if isinstance(how, Completed) and len(how.output.encode()) > TEAM_CONSTANTS.inline_cap_bytes:
        big = await put_text(rt, how.output)

    def decide(tx: DecideTx) -> Sequence[Draft] | Refusal[None]:
        kept = after_barrier(tx.fold, drafts)
        batch = Batch(tx.fold.seq, tx.now, team.mint)
        provenance = turn_provenance(tx.conn, tx.fold.events)
        ctx = SettleContext(
            tx.conn, batch, _thread(rt), rt.writer.branch_id, provenance, lambda _t: big
        )
        parked = any(isinstance(e, ParkedEvent) for e in tx.fold.events)
        for d in kept:
            got = batch.add(d)
            if d.type == "parked" and not parked and provenance is not None:
                parked = True
                park_notice(ctx, provenance, got, str(d.data["reason"]))
        closes = any(d.type == "turn_completed" for d in kept)
        if how is not None and provenance is not None and closes:
            settle(ctx, how)
        return batch.drafts

    done = await rt.append_decided(decide)
    if isinstance(done, Refusal):
        raise AssertionError("a settlement never refuses")
    return done


async def consume_mail(rt: Runtime) -> Halt | None:
    """mail.consume under this writer: a receipt that opens a turn leaves the loop a turn."""
    team = _team(rt)

    def decide(tx: DecideTx) -> Sequence[Draft] | Refusal[None]:
        batch = Batch(tx.fold.seq, tx.now, team.mint)
        branch = rt.writer.branch_id
        consume(ConsumeContext(tx.conn, batch, _thread(rt), branch, tx.fold, team.principal))
        return batch.drafts

    done = await rt.append_decided(decide)
    return lost(done.error) if isinstance(done, Err) else None


async def team_turns(rt: Runtime, halt: Halt, turn: Callable[[], Awaitable[Halt]]) -> Halt:
    """A team thread's turns after its input's, until nothing opens one (a member) or its run
    ends (a lead), woken by its members' progress, its own log moving, or the in-process poll."""
    team = _team(rt)
    while isinstance(halt, Idle) or (isinstance(halt, Parked) and _on_members(rt)):
        waits = [] if team.progress is None else [team.progress(), rt.writer.moved()]
        tasks = [asyncio.ensure_future(w) for w in waits]
        try:
            stopped = await consume_mail(rt)
            if stopped is not None:
                return stopped
            if rt.fold.in_turn:
                halt = await turn()
            elif not tasks or not _waits(rt, team):
                return _idle_or_parked(rt, halt)
            else:
                poll = TEAM_CONSTANTS.wake_poll_in_process_ms / 1000
                await asyncio.wait(tasks, timeout=poll, return_when=asyncio.FIRST_COMPLETED)
                for task in tasks:
                    if task.done() and not task.cancelled():
                        task.result()  # a worker bug surfaces here
        finally:
            for task in tasks:
                task.cancel()
    return halt


def _on_members(rt: Runtime) -> bool:
    return all(p.kind == "member" for p in rt.fold.parked)


def _waits(rt: Runtime, team: TeamRuntime) -> bool:
    """A lead parked only on its members waits while one of them runs; an idle one while its
    run is open."""
    if rt.fold.parked:
        return _on_members(rt) and team.busy is not None and team.busy()
    return run_status(rt.events) == "running"


def _idle_or_parked(rt: Runtime, halt: Halt) -> Halt:
    if rt.fold.parked:
        return parked(rt.events, rt.fold.parked)
    return halt if isinstance(halt, Idle) else Idle("end_turn")
