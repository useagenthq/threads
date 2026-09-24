"""ReducedState.transcript: the conversational entries the next Render v1 request renders.

Only which events render, and in what order, matters here, so no artifact is read. The render
rules are spec/schema/README.md, "Render v1".
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from threads.log import (
    CompactedEvent,
    Event,
    EventId,
    HeartbeatEvent,
    InjectedEvent,
    MessageReceivedEvent,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    SteerEvent,
    ThreadStartedEvent,
    ToolResultEvent,
    ToolResultLateEvent,
    UserInputEvent,
)
from threads.reduce.view import RenderView, assistant_parts, render_view, walk

type Role = Literal["user", "context", "assistant", "tool", "summary"]


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    role: Role
    event_id: EventId


# Yielded by the walk only in place of its range, a CompactedEvent is the summary.
_ROLES: Mapping[type, Role] = {
    InjectedEvent: "context",
    HeartbeatEvent: "context",
    MessageReceivedEvent: "context",
    ToolResultEvent: "tool",
    ToolResultLateEvent: "tool",
    CompactedEvent: "summary",
}


def _role(view: RenderView, event: Event) -> Role | None:
    if isinstance(event, UserInputEvent | SteerEvent):
        return None if event.event_id in view.denied else "user"
    if isinstance(event, ModelResponseEvent | ModelResponseRecoveredEvent):
        return "assistant" if assistant_parts(view, event) else None
    return _ROLES.get(type(event))


def transcript(events: Sequence[Event]) -> tuple[TranscriptEntry, ...]:
    if not any(isinstance(e, ThreadStartedEvent) for e in events):
        return ()  # a team log never renders
    view = render_view(events)
    entries: list[TranscriptEntry] = []
    for event in walk(view, events):
        role = _role(view, event)
        if role is not None:
            entries.append(TranscriptEntry(role, event.event_id))
    return tuple(entries)
