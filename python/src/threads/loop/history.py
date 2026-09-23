"""Pure reads of the folded log that the loop decides from. Nothing here is stored state: every
counter (attempts, retries, waits, continuations) is derived from events, so a resumed run and
an uninterrupted one decide alike (invariant 1)."""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    ApprovalDeniedEvent,
    ApprovalGrantedEvent,
    ApprovalRequestedEvent,
    ArtifactRef,
    CallId,
    CompactedEvent,
    CompactionFailedEvent,
    EffectBeginEvent,
    EffectCommitEvent,
    EffectResolvedEvent,
    EffectUnknownEvent,
    Event,
    EventId,
    InjectedEvent,
    ModelAttemptAbandonedEvent,
    ModelRequestEvent,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    PermissionDecisionEvent,
    SteerEvent,
    ToolCallEvent,
    ToolResultEvent,
    UserInputEvent,
)

CONTINUE_TEXT: Final = (
    "Output limit reached. Continue exactly where you stopped. Do not repeat earlier output."
)
"""The fixed max-output continuation."""


def turn_events(events: Sequence[Event]) -> Sequence[Event]:
    """The open turn's events, from its `user_input` on."""
    for index in range(len(events) - 1, -1, -1):
        if isinstance(events[index], UserInputEvent):
            return events[index:]
    return ()


def _side(events: Sequence[Event]) -> frozenset[EventId]:
    return frozenset(
        e.event_id
        for e in events
        if isinstance(e, ModelRequestEvent) and e.data.purpose == "compaction"
    )


def _opens_step(event: Event, side: frozenset[EventId]) -> bool:
    if isinstance(event, UserInputEvent | SteerEvent | InjectedEvent | ToolResultEvent):
        return True
    turn = isinstance(event, ModelResponseEvent | ModelResponseRecoveredEvent)
    return turn and event.data.request_event_id not in side


def step_events(events: Sequence[Event]) -> Sequence[Event]:
    """The current step: everything after the last event that asks for a new model request (an
    input, an injection, a tool result, a turn response), that event included."""
    side = _side(events)
    for index in range(len(events) - 1, -1, -1):
        if _opens_step(events[index], side):
            return events[index:]
    return events


@dataclass(frozen=True, slots=True)
class Step:
    """Counters of one logical request across its attempts."""

    attempts: int
    """Turn model requests so far; the next attempt is this plus one."""
    retryable: int
    """Rate-limited, overloaded and server-error rejections."""
    overloaded_run: int
    """Consecutive overloaded rejections at the end, for fallback."""
    crashes: int
    """Attempts abandoned with an unknown outcome (the crash re-send budget)."""
    compacted: bool
    """A reactive compaction already ran (the once-per-step guard of L4 and L5)."""


def step(events: Sequence[Event]) -> Step:
    current = step_events(events)
    side = _side(events)
    attempts = retryable = run = crashes = 0
    compacted = False
    for event in current:
        if isinstance(event, ModelRequestEvent) and event.event_id not in side:
            attempts += 1
        elif isinstance(event, CompactedEvent | CompactionFailedEvent):
            compacted = True
        elif (
            isinstance(event, ModelAttemptAbandonedEvent)
            and event.data.request_event_id not in side
        ):
            reason = event.data.reason
            retryable += reason in ("rate_limited", "overloaded", "server_error")
            run = run + 1 if reason == "overloaded" else 0
            crashes += event.data.provider_outcome == "unknown"
    return Step(attempts, retryable, run, crashes, compacted)


def continuations(events: Sequence[Event]) -> int:
    """Max-output continuations appended in the open turn."""
    return sum(
        1
        for e in turn_events(events)
        if isinstance(e, InjectedEvent)
        and e.data.source == "recovery"
        and e.data.text == CONTINUE_TEXT
    )


def pauses(events: Sequence[Event]) -> int:
    """Turn responses in the open turn that stopped with pause_turn."""
    side = _side(events)
    return sum(
        1
        for e in turn_events(events)
        if isinstance(e, ModelResponseEvent | ModelResponseRecoveredEvent)
        and e.data.request_event_id not in side
        and e.data.stop_reason == "pause_turn"
    )


@dataclass(frozen=True, slots=True)
class CallState:
    """Where one call stands: its authorization, its challenge and its effect attempts."""

    call: ToolCallEvent
    decision: str | None = None
    challenge: ApprovalRequestedEvent | None = None
    approved: bool | None = None
    begins: tuple[EffectBeginEvent, ...] = ()
    effect: str | None = None
    """The last effect event's status: begun, committed, unknown or a resolved outcome."""
    commit: ArtifactRef | None = None
    unknown_reason: str | None = None


def call_state(events: Sequence[Event], call_id: CallId) -> CallState:
    """The call's state from its own events. A missing call is a bug: callers pass pending ids."""
    call = next(e for e in events if isinstance(e, ToolCallEvent) and e.data.call_id == call_id)
    state = CallState(call)
    for event in events[events.index(call) :]:
        state = _fold_call(state, event, call_id)
    return state


def _fold_call(s: CallState, event: Event, call_id: CallId) -> CallState:  # noqa: PLR0911
    match event:
        case PermissionDecisionEvent() if event.data.call_id == call_id:
            return replace(s, decision=event.data.decision)
        case ApprovalRequestedEvent() if event.data.call_id == call_id:
            return replace(s, challenge=event)
        case ApprovalGrantedEvent() | ApprovalDeniedEvent() if event.data.call_id == call_id:
            return replace(s, approved=isinstance(event, ApprovalGrantedEvent))
        case EffectBeginEvent() if event.data.call_id == call_id:
            return replace(s, begins=(*s.begins, event), effect="begun")
        case EffectCommitEvent() if event.data.call_id == call_id:
            return replace(s, effect="committed", commit=event.data.result_ref)
        case EffectUnknownEvent() if event.data.call_id == call_id:
            return replace(s, effect="unknown", unknown_reason=event.data.reason)
        case EffectResolvedEvent() if event.data.call_id == call_id:
            ref = None if event.data.result_ref is MISSING else event.data.result_ref
            return replace(s, effect=event.data.outcome, commit=ref)
        case _:
            return s
