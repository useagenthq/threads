"""The turn a saved case replays (spec lane 22, A.1): a completed turn, found by its user_input,
the last completed one by default. Its run's leading session_start decisions and its trailing
observation decisions belong to it, so the rerun reproduces them too. The case log is the branch
through the event before the run began."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from threads.evals.kinds import USER_EVENTS
from threads.evals.offline import CHILD_TOOLS, TEAM_TOOLS
from threads.log import (
    EffectBeginEvent,
    EffectCommitEvent,
    EffectResolvedEvent,
    Event,
    EventId,
    HookDecisionEvent,
    InjectedEvent,
    ParseError,
    SnapshotEvent,
    ToolCallEvent,
    TurnCompletedEvent,
    UnknownEvent,
    UserInputEvent,
)
from threads.result import Err, Ok
from threads.store import StoredEvent


@dataclass(frozen=True, slots=True)
class Turn:
    restore_seq: int
    """The seq the case log ends at: the last event before the turn's run appended anything."""
    input: UserInputEvent
    events: tuple[Event, ...]
    """What the turn's run appended, in order: what a rerun must append again."""
    unknown: tuple[str, ...]
    """Event types in the turn this build doesn't know: user code the rerun can't script."""


def _invalid(message: str) -> Err[ParseError]:
    return Err(ParseError("invalid_request", message))


def _input_at(events: Sequence[Event], at: EventId | None) -> Ok[int] | Err[ParseError]:
    """The user_input `at` names: the input itself, or the first input after a snapshot."""
    if at is None:
        done = max(
            (i for i, e in enumerate(events) if isinstance(e, TurnCompletedEvent)), default=-1
        )
        found = [i for i, e in enumerate(events[:done]) if isinstance(e, UserInputEvent)]
        return Ok(found[-1]) if done >= 0 and found else _invalid("no completed turn to save")
    named = next((i for i, e in enumerate(events) if e.event_id == at), -1)
    event = events[named] if named >= 0 else None
    if isinstance(event, UserInputEvent):
        return Ok(named)
    if isinstance(event, SnapshotEvent):
        after = [i for i, e in enumerate(events) if i > named and isinstance(e, UserInputEvent)]
        if after:
            return Ok(after[0])
    return _invalid(f"no turn at {at}: name its user_input or a snapshot before it")


def _leading(events: Sequence[Event], input: int) -> int:
    """session_start decisions (and their injections) the turn's run appended before its input."""
    start = input
    while start > 0:
        e = events[start - 1]
        session = isinstance(e, HookDecisionEvent) and e.data.hook == "session_start"
        injection = isinstance(e, InjectedEvent) and e.data.source == "hook"
        if not session and not injection:
            break
        start -= 1
    opened = any(isinstance(e, HookDecisionEvent) for e in events[start:input])
    return start if opened else input


def _trailing(events: Sequence[Event], done: int) -> int:
    """The observation decisions the run appended once the turn ended (stop_failure, ...)."""
    end = done
    while end + 1 < len(events) and isinstance(events[end + 1], HookDecisionEvent):
        end += 1
    return end


def find_turn(stored: Sequence[StoredEvent], at: EventId | None) -> Ok[Turn] | Err[ParseError]:
    events = [e for e in stored if not isinstance(e, UnknownEvent)]
    found = _input_at(events, at)
    if isinstance(found, Err):
        return found
    index = found.value
    input = events[index]
    if not isinstance(input, UserInputEvent):
        raise AssertionError("_input_at finds a user_input")
    ends = (
        i
        for i, e in enumerate(events)
        if i > index and isinstance(e, TurnCompletedEvent | UserInputEvent)
    )
    done = next(ends, -1)
    if done < 0 or not isinstance(events[done], TurnCompletedEvent):
        return _invalid(f"the turn at {input.event_id} is not completed")
    start, end = _leading(events, index), _trailing(events, done)
    first, last = events[start].seq, events[end].seq
    unknown = [e.type for e in stored if isinstance(e, UnknownEvent) and first <= e.seq <= last]
    return Ok(Turn(first - 1, input, tuple(events[start : end + 1]), tuple(dict.fromkeys(unknown))))


_CHILD_EVENTS: Final = frozenset({"agent_spawned", "handoff", "team_opened", "member_started"})
"""A child thread's model calls aren't in the case; neither are a team's member threads."""
_TEAM_EVENTS: Final = frozenset({"message_received", "woken"})


def _called(turn: Turn, names: frozenset[str]) -> bool:
    return any(isinstance(e, ToolCallEvent) and e.data.name in names for e in turn.events)


def _unsettled(turn: Turn, later: Sequence[Event]) -> bool:
    """An effect the turn began that nothing settled: no result to stub."""
    settled = {
        e.data.call_id for e in later if isinstance(e, EffectCommitEvent | EffectResolvedEvent)
    }
    return any(
        isinstance(e, EffectBeginEvent) and e.data.call_id not in settled for e in turn.events
    )


def _unscripted(turn: Turn) -> tuple[str, ...]:
    """Injected sources no list classifies: user code this build can't script."""
    known = {
        *USER_EVENTS.scriptable.sources,
        *USER_EVENTS.framework.sources,
        *USER_EVENTS.child_thread.sources,
    }
    sources = [
        f"injected:{e.data.source}"
        for e in turn.events
        if isinstance(e, InjectedEvent) and e.data.source not in known
    ]
    return (*turn.unknown, *dict.fromkeys(sources))


@dataclass(frozen=True, slots=True)
class Offline:
    reason: str
    types: tuple[str, ...] | None = None


def offline_reason(turn: Turn, later: Sequence[Event]) -> Offline | None:
    """Why the turn can't rerun offline from the case alone (artifact_missing is found on copy)."""
    text = turn.input.data.text
    if not isinstance(text, str):
        return Offline("content_input")
    if _called(turn, CHILD_TOOLS) or any(e.type in _CHILD_EVENTS for e in turn.events):
        return Offline("child_threads")
    if _called(turn, TEAM_TOOLS) or any(e.type in _TEAM_EVENTS for e in turn.events):
        return Offline("team_calls")
    if _unsettled(turn, later):
        return Offline("unsettled_effect")
    types = _unscripted(turn)
    return Offline("extension_events", types) if types else None
