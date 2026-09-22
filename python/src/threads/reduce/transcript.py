"""ReducedState.transcript: the conversational entries the next Render v1 request renders.

Only which events render, and in what order, matters here, so no artifact is read. The render
rules are spec/schema/README.md, "Render v1".
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from threads.log import (
    CompactedEvent,
    Event,
    EventId,
    HeartbeatEvent,
    HookDecisionEvent,
    HostedToolPart,
    InjectedEvent,
    ModelRequestEvent,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    ReasoningPart,
    SettingsChangedEvent,
    SteerEvent,
    ToolResultEvent,
    ToolResultLateEvent,
    UserInputEvent,
)

type Role = Literal["user", "context", "assistant", "tool", "summary"]


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    role: Role
    event_id: EventId


@dataclass(frozen=True, slots=True)
class _View:
    denied: frozenset[EventId]
    """Inputs a before_input hook denied or failed on: they render nothing."""
    side: frozenset[EventId]
    """Compaction side requests: their responses render nothing."""
    cut: int
    """Reasoning and hosted-tool parts before this seq are omitted (omit_prior)."""
    ranges: tuple[CompactedEvent, ...]
    """Outermost compacted ranges; a compacted event inside a later range is dropped too."""


def _view(events: Sequence[Event]) -> _View:
    denied: set[EventId] = set()
    side: set[EventId] = set()
    cut = 0
    compactions: list[CompactedEvent] = []
    for event in events:
        if isinstance(event, HookDecisionEvent) and event.data.hook == "before_input":
            if event.data.decision in ("deny", "failed") and isinstance(
                event.data.input_event_id, str
            ):
                denied.add(event.data.input_event_id)
        elif isinstance(event, ModelRequestEvent) and event.data.purpose == "compaction":
            side.add(event.event_id)
        elif isinstance(event, SettingsChangedEvent):
            if event.data.settings.reasoning_carryover == "omit_prior":
                cut = event.seq
        elif isinstance(event, CompactedEvent):
            compactions.append(event)
    outer = tuple(c for c in compactions if not any(_covers(o, c.seq) for o in compactions))
    return _View(frozenset(denied), frozenset(side), cut, outer)


def _covers(compacted: CompactedEvent, seq: int) -> bool:
    return compacted.data.from_seq <= seq <= compacted.data.to_seq


def _role(view: _View, event: Event) -> Role | None:
    if isinstance(event, UserInputEvent | SteerEvent):
        return None if event.event_id in view.denied else "user"
    if isinstance(event, InjectedEvent | HeartbeatEvent):
        return "context"
    if isinstance(event, ModelResponseEvent | ModelResponseRecoveredEvent):
        if event.data.request_event_id in view.side:
            return None
        opaque = (ReasoningPart, HostedToolPart) if event.seq < view.cut else ()
        kept = [part for part in event.data.content if not isinstance(part, opaque)]
        return "assistant" if kept else None
    if isinstance(event, ToolResultEvent | ToolResultLateEvent):
        return "tool"
    return None


def transcript(events: Sequence[Event]) -> tuple[TranscriptEntry, ...]:
    view = _view(events)
    entries: list[TranscriptEntry] = []
    for event in events:
        compacted = next((c for c in view.ranges if _covers(c, event.seq)), None)
        if compacted is None:
            role = _role(view, event)
            if role is not None:
                entries.append(TranscriptEntry(role, event.event_id))
        elif event.seq == compacted.data.from_seq:
            # The summary stands in place of the range's first event.
            entries.append(TranscriptEntry("summary", compacted.event_id))
    return tuple(entries)
