"""The loop: decide the next step from the folded log, append it, repeat until the turn ends or
the run must stop. No decision reads memory the log doesn't hold, so resuming after a crash is
the same loop over the same log (invariant 1)."""

from collections.abc import Sequence
from typing import TYPE_CHECKING, assert_never

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    AgentFinishedEvent,
    AgentSpawnedEvent,
    CancelRequestedEvent,
    CompactionFailedEvent,
    ContextEditedEvent,
    Event,
    HandoffEvent,
    HookDecisionEvent,
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
    TeamMessageEvent,
    TeamTaskClaimedEvent,
    TeamTaskCreatedEvent,
    TeamTaskUpdatedEvent,
    ToolResultEvent,
    ToolResultLateEvent,
    TurnCompletedEvent,
)
from threads.loop import calls, gates, output, parallel, record, retries, tool_gates
from threads.loop.defaults import context, max_pauses
from threads.loop.drafts import draft
from threads.loop.history import (
    CONTINUE_TEXT,
    continuations,
    open_cancel,
    pauses,
    step,
    turn_events,
)
from threads.loop.runtime import Halt, Idle, Parked, Runtime, lost
from threads.loop.turn import complete, request
from threads.reduce.fold import call_spec
from threads.result import Err

if TYPE_CHECKING:
    from pydantic import JsonValue

_NEUTRAL = (
    AgentSpawnedEvent,
    AgentFinishedEvent,
    ToolResultLateEvent,
    TeamTaskCreatedEvent,
    TeamTaskClaimedEvent,
    TeamTaskUpdatedEvent,
    TeamMessageEvent,
    HandoffEvent,
    LogRepairedEvent,
    ParkedEvent,
    ParkEscalatedEvent,
    ResumedEvent,
    OutputValidatedEvent,
    HookDecisionEvent,
    ContextEditedEvent,
)
"""Events that never decide what comes next: a gate re-reads its own decisions from the log, and
team and child records can land between any two steps."""


async def drive(rt: Runtime) -> Halt:
    """Runs until the open turn ends (`Idle`), the branch parks, or the run fails. The run keeps
    the lease renewed meanwhile (threads.agents.run)."""
    while True:
        flushed = None if rt.framework is None else await rt.framework.flush(rt)
        halt = flushed or await _step(rt)
        if halt is not None:
            if isinstance(halt, Parked):
                await _notify_parked(rt)
            return halt


async def _step(rt: Runtime) -> Halt | None:
    fold = rt.fold
    if fold.parked:
        return parked(rt.events, fold.parked)
    if not fold.in_turn:
        return Idle(_last_reason(rt.events))
    cancel = open_cancel(rt.events)
    if cancel is not None:
        return await _cancel(rt, cancel)
    owed = record.owed(rt)
    if fold.pending or owed:
        gated = await gates.after_model(rt)
        if gated is not None:
            return None if gated == gates.AGAIN else gated
        # Every call of the response is recorded and authorized before any of them runs.
        return await (record.record_calls(rt) if owed else parallel.run_pending(rt))
    return await _next(rt)


async def _notify_parked(rt: Runtime) -> None:
    """notification observers: the branch parked."""
    last = next((e for e in reversed(rt.events) if isinstance(e, ParkedEvent)), None)
    if last is not None:
        await gates.observe(rt, "notification", last)


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


async def _cancel(rt: Runtime, cancel: CancelRequestedEvent) -> Halt | None:
    """The durable barrier: never-begun calls close as not_executed, then
    `cancelled`. An effect still in doubt is settled or parked first, never cancelled over."""
    for call_id in list(rt.fold.pending):
        halt = await calls.cancel_call(rt, call_id)
        if halt is not None:
            return halt
    halt = await record.close_unrecorded(rt)
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
        # A requested compaction's abandoned side attempt is the ladder's to decide (after a
        # crash), never the turn's retry.
        case ModelAttemptAbandonedEvent() if rt.fold.compaction_request is None:
            return await retries.after_abandon(rt, last, current)
        case RetryScheduledEvent():
            await rt.wait_until(last.data.not_before)
            return await request(rt, current.attempts + 1)
        case CompactionFailedEvent() if last.data.cause_event_id is MISSING:
            return await complete(rt, "context_exhausted")
        case _:
            return await request(rt, current.attempts + 1)


async def _after_response(
    rt: Runtime, response: ModelResponseEvent | ModelResponseRecoveredEvent
) -> Halt | None:
    """spec/schema/README.md, "Turn endings by stop_reason", once after_model released it."""
    gated = await gates.after_model(rt)
    if gated is not None:
        return None if gated == gates.AGAIN else gated
    stop = response.data.stop_reason
    match stop:
        case "end_turn" | "stop_sequence" | "refusal" | "tool_use":
            # Under an output schema the turn ends through final_output, never in plain text.
            return await (output.ask(rt) if output.pinned(rt.fold) else gates.end_turn(rt))
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
    """A successful `ends_turn` result ends the turn with no further model call. Results pass
    their guardrail first (before_tool_result)."""
    gated = await tool_gates.before_results(rt)
    if gated is not None:
        return None if gated == gates.AGAIN else gated
    turn = turn_events(rt.events)
    for event in reversed(turn):
        if not isinstance(event, ToolResultEvent):
            break
        spec = call_spec(rt.fold, event.data.call_id)
        if spec is not None and spec.ends_turn is True and not event.data.is_error:
            return await gates.end_turn(rt)
    gated = await tool_gates.after_batch(rt)
    if gated is not None:
        return None if gated == gates.AGAIN else gated
    return await request(rt, 1)
