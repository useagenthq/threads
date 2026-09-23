"""The loop: decide the next step from the folded log, append it, repeat until the turn ends or
the run must stop. No decision reads memory the log doesn't hold, so resuming after a crash is
the same loop over the same log (invariant 1)."""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final, assert_never

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    CancelledEvent,
    CancelRequestedEvent,
    CompactionFailedEvent,
    Event,
    LogRepairedEvent,
    ModelAttemptAbandonedEvent,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    OutputValidatedEvent,
    ParkAddress,
    ParkedEvent,
    ParkEscalatedEvent,
    ResumedEvent,
    RetryScheduledEvent,
    ToolResultEvent,
    TurnCompletedEvent,
)
from threads.loop import calls, retries
from threads.loop.defaults import context, max_pauses
from threads.loop.drafts import draft
from threads.loop.history import CONTINUE_TEXT, continuations, pauses, step, turn_events
from threads.loop.runtime import Halt, Idle, Parked, Runtime, lost
from threads.loop.turn import complete, request
from threads.reduce.fold import policy
from threads.result import Err

if TYPE_CHECKING:
    from pydantic import JsonValue

RENEW_MS: Final = 10_000
"""Lease renewal interval."""
_NEUTRAL = (LogRepairedEvent, ParkedEvent, ParkEscalatedEvent, ResumedEvent, OutputValidatedEvent)
"""Events that never decide what comes next."""


async def drive(rt: Runtime) -> Halt:
    """Runs until the open turn ends (`Idle`), the branch parks, or the run fails."""
    renewed = rt.clock()
    while True:
        if rt.clock() - renewed >= RENEW_MS:
            kept = await rt.writer.renew()
            if isinstance(kept, Err):
                return lost(kept.error)
            renewed = rt.clock()
        halt = await _step(rt)
        if halt is not None:
            return halt


async def _step(rt: Runtime) -> Halt | None:
    fold = rt.fold
    if fold.parked:
        return parked(rt.events, fold.parked)
    if not fold.in_turn:
        return Idle(_last_reason(rt.events))
    cancel = _open_cancel(rt.events)
    if cancel is not None:
        return await _cancel(rt, cancel)
    if fold.pending:
        return await calls.run_call(rt, fold.pending[0])
    return await _next(rt)


def parked(events: Sequence[Event], open_addresses: Sequence[ParkAddress]) -> Parked:
    """The run's parked result: the latest open park's reason, and every open address."""
    last = next(
        e
        for e in reversed(events)
        if isinstance(e, ParkedEvent) and e.data.address in open_addresses
    )
    return Parked(last.data.reason, tuple(open_addresses))


def _last_reason(events: Sequence[Event]) -> str:
    done = next((e for e in reversed(events) if isinstance(e, TurnCompletedEvent)), None)
    return "end_turn" if done is None else done.data.reason


def _open_cancel(events: Sequence[Event]) -> CancelRequestedEvent | None:
    for event in reversed(turn_events(events)):
        if isinstance(event, CancelledEvent):
            return None
        if isinstance(event, CancelRequestedEvent):
            return event
    return None


async def _cancel(rt: Runtime, cancel: CancelRequestedEvent) -> Halt | None:
    """The durable barrier: never-begun calls close as not_executed, then
    `cancelled`. An effect still in doubt is settled or parked first, never cancelled over."""
    for call_id in list(rt.fold.pending):
        halt = await calls.cancel_call(rt, call_id)
        if halt is not None:
            return halt
    data = {"request_event_id": cancel.event_id}
    done = await rt.append(
        draft("cancelled", data), draft("turn_completed", {"reason": "cancelled"})
    )
    return lost(done.error) if isinstance(done, Err) else None


def _decisive(events: Sequence[Event]) -> Event:
    return next(e for e in reversed(turn_events(events)) if not isinstance(e, _NEUTRAL))


async def _next(rt: Runtime) -> Halt | None:
    """No call is pending: what the last decisive event of the turn asks for."""
    last = _decisive(rt.events)
    current = step(rt.events)
    match last:
        case ModelResponseEvent() | ModelResponseRecoveredEvent():
            return await _after_response(rt, last)
        case ToolResultEvent():
            return await _after_results(rt)
        case ModelAttemptAbandonedEvent():
            return await retries.after_abandon(rt, last, current)
        case RetryScheduledEvent():
            await rt.wait_until(last.data.not_before)
            return await request(rt, current.attempts + 1)
        case CompactionFailedEvent():
            return await complete(rt, "context_exhausted")
        case _:
            return await request(rt, current.attempts + 1)


async def _after_response(
    rt: Runtime, response: ModelResponseEvent | ModelResponseRecoveredEvent
) -> Halt | None:
    """spec/schema/README.md, "Turn endings by stop_reason"."""
    stop = response.data.stop_reason
    match stop:
        case "end_turn" | "stop_sequence" | "refusal" | "tool_use":
            return await complete(rt, "end_turn")
        case "max_tokens":
            return await _continue_output(rt)
        case "context_window_exceeded":
            return await complete(rt, "context_exhausted")
        case "pause_turn" if pauses(rt.events) <= max_pauses(rt.fold):
            # Ask again with nothing added, so the paused content goes back as-is.
            return await request(rt, 1)
        case "pause_turn" | "other":
            return await complete(rt, "error")
        case _:
            assert_never(stop)


async def _continue_output(rt: Runtime) -> Halt | None:
    """ask to continue, up to max_output_continuations per turn."""
    if continuations(rt.events) >= context(rt.fold).max_output_continuations:
        return await complete(rt, "max_output")
    data: dict[str, JsonValue] = {
        "source": "recovery",
        "trust": "trusted_instruction",
        "origin": {"id": "max_output"},
        "text": CONTINUE_TEXT,
    }
    done = await rt.append(draft("injected", data))
    return lost(done.error) if isinstance(done, Err) else None


async def _after_results(rt: Runtime) -> Halt | None:
    """A successful `ends_turn` result ends the turn with no further model call; too many
    rejected output candidates end it output_invalid."""
    turn = turn_events(rt.events)
    names = {c.data.call_id: c.data.name for c in rt.fold.calls.values()}
    for event in reversed(turn):
        if not isinstance(event, ToolResultEvent):
            break
        spec = rt.fold.tools.get(names.get(event.data.call_id, ""))
        if spec is not None and spec.ends_turn is True and not event.data.is_error:
            return await complete(rt, "end_turn")
    pinned = policy(rt.fold)
    rejected = sum(
        1 for e in turn if isinstance(e, OutputValidatedEvent) and e.data.outcome == "rejected"
    )
    if pinned is not None and pinned.output is not MISSING and rejected > pinned.output.max_retries:
        return await complete(rt, "output_invalid")
    return await request(rt, 1)
