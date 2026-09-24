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
from threads.reduce import Fold
from threads.reduce.fold import loop_parked
from threads.reduce.run_end import RunStatus, run_end
from threads.result import Err
from threads.store import Draft
from threads.store.writer import DecideTx, Refusal
from threads.team.batch import Batch
from threads.team.close import reader_of
from threads.team.constants import TEAM_CONSTANTS
from threads.team.consume import ConsumeContext, consume
from threads.team.deadline import deadline, due_ids
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


async def _decided(rt: Runtime, step: Callable[[ConsumeContext], object]) -> Halt | None:
    """One team step decided under this writer."""
    team = _team(rt)

    def decide(tx: DecideTx) -> Sequence[Draft] | Refusal[None]:
        batch = Batch(tx.fold.seq, tx.now, team.mint)
        branch = rt.writer.branch_id
        read = reader_of(tx.read)
        step(ConsumeContext(tx.conn, batch, _thread(rt), branch, tx.fold, read, team.principal))
        return batch.drafts

    done = await rt.append_decided(decide)
    return lost(done.error) if isinstance(done, Err) else None


async def team_step(rt: Runtime) -> Halt | None:
    """One step between turns: the pending mail (a receipt that opens a turn, or an answer that
    resumes one, leaves the loop a turn to run), then every ask or wait whose deadline passed."""

    def deadlines(ctx: ConsumeContext) -> None:
        for ident in due_ids(ctx.conn, ctx.fold, ctx.branch_id, ctx.batch.now):
            deadline(ctx, ident)

    stopped = await _decided(rt, consume)
    return stopped if stopped is not None else await _decided(rt, deadlines)


_TEAM_PARKS = frozenset({"member", "ask", "wait"})
"""Parks that team mail or a deadline resolves: on a member, an ask or a wait."""


def on_team(fold: Fold) -> bool:
    """Whether every park of this thread is one team mail or a deadline resolves."""
    return all(p.kind in _TEAM_PARKS for p in loop_parked(fold))


async def team_turns(rt: Runtime, halt: Halt, turn: Callable[[], Awaitable[Halt]]) -> Halt:
    """A team thread's turns after its input's: while idle, or parked only on team parks, its
    pending mail is consumed and its due deadlines closed, and each turn that opens or resumes is
    run. A member run by the team worker stops once nothing opens a turn; the lead of an
    in-process run waits until its run ends, woken by its members' progress, its own log moving,
    or the in-process poll; parked on an ask or a wait, until it is answered or due."""
    team = _team(rt)
    while isinstance(halt, Idle) or (isinstance(halt, Parked) and on_team(rt.fold)):
        waits = [] if team.progress is None else [team.progress(), rt.writer.moved()]
        tasks = [asyncio.ensure_future(w) for w in waits]
        try:
            stopped = await team_step(rt)
            if stopped is not None:
                return stopped
            if rt.fold.in_turn and not loop_parked(rt.fold):
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


def _waits(rt: Runtime, team: TeamRuntime) -> bool:
    """The lead waits: on an ask or a wait (a deadline bounds it), on members the worker is
    running, or, idle, while its run is open."""
    parked = loop_parked(rt.fold)
    if any(p.kind in ("ask", "wait") for p in parked):
        return True
    if parked:
        return team.busy is not None and team.busy()
    return run_status(rt.events) == "running"


def _idle_or_parked(rt: Runtime, halt: Halt) -> Halt:
    if held := loop_parked(rt.fold):
        return parked(rt.events, held)
    return halt if isinstance(halt, Idle) else Idle("end_turn")
