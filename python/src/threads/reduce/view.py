"""One pass over the events before a request: what Render v1 drops, rewrites or reorders.

Shared by the transcript projection and the renderer, so both agree on which events render
and in what order (spec/schema/README.md, "Render v1").
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    CallId,
    CompactedEvent,
    ContextEditedEvent,
    Event,
    EventId,
    HookDecisionEvent,
    HostedToolPart,
    InjectedEvent,
    ModelRequestEvent,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    OutputPart,
    Principal,
    ReasoningPart,
    SettingsChangedEvent,
    Span,
    SteerEvent,
    ToolCallEvent,
    ToolResultEvent,
    ToolResultLateEvent,
    ToolsChangedEvent,
    ToolUsePart,
    UserInputEvent,
)


@dataclass(frozen=True, slots=True)
class RenderView:
    denied: frozenset[EventId]
    """Inputs a before_input hook denied or failed on: they render nothing."""
    side: frozenset[EventId]
    """Compaction side requests: their responses render nothing."""
    cut: int
    """Reasoning and hosted-tool parts before this seq are omitted (omit_prior)."""
    ranges: tuple[CompactedEvent, ...]
    """Outermost compacted ranges; a compacted event inside a later range is dropped too."""
    cleared: frozenset[CallId]
    """Tool results a context_edited cleared."""
    redactions: Mapping[tuple[CallId, int], tuple[Span, ...]]
    """UTF-8 byte spans to redact, per (call_id, part index)."""
    host_calls: frozenset[CallId]
    """Calls no model response proposed (a host's channel_send reply): their results render
    nothing, since the model never asked for them."""
    withheld: frozenset[EventId] = frozenset()
    """Recalled memory from a turn another principal started: memory is per principal, so in a shared thread it renders only while that principal's input is current."""


@dataclass(slots=True)
class _Builder:
    denied: set[EventId] = field(default_factory=set[EventId])
    side: set[EventId] = field(default_factory=set[EventId])
    cut: int = 0
    compactions: list[CompactedEvent] = field(default_factory=list[CompactedEvent])
    cleared: set[CallId] = field(default_factory=set[CallId])
    redactions: dict[tuple[CallId, int], tuple[Span, ...]] = field(
        default_factory=dict[tuple[CallId, int], tuple[Span, ...]]
    )
    proposed: set[CallId] = field(default_factory=set[CallId])
    calls: list[CallId] = field(default_factory=list[CallId])
    principal: Principal | None = None
    recalled: list[tuple[EventId, Principal | None]] = field(
        default_factory=list[tuple[EventId, Principal | None]]
    )

    def add(self, event: Event) -> None:
        self._memory(event)
        if isinstance(event, ModelResponseEvent | ModelResponseRecoveredEvent):
            self.proposed.update(
                p.call_id for p in event.data.content if isinstance(p, ToolUsePart)
            )
        elif isinstance(event, ToolCallEvent):
            self.calls.append(event.data.call_id)
        if isinstance(event, HookDecisionEvent):
            self._hook(event)
        elif isinstance(event, ModelRequestEvent) and event.data.purpose == "compaction":
            self.side.add(event.event_id)
        elif isinstance(event, SettingsChangedEvent):
            if event.data.settings.reasoning_carryover == "omit_prior":
                self.cut = event.seq
        elif isinstance(event, ContextEditedEvent):
            self._edit(event)
        elif isinstance(event, CompactedEvent):
            self.compactions.append(event)

    def _memory(self, event: Event) -> None:
        if isinstance(event, UserInputEvent | SteerEvent):
            self.principal = event.actor.principal
        elif isinstance(event, InjectedEvent) and event.data.source == "memory":
            self.recalled.append((event.event_id, self.principal))

    def _hook(self, event: HookDecisionEvent) -> None:
        d = event.data
        denies = d.hook == "before_input" and d.decision in ("deny", "failed")
        if denies and d.input_event_id is not MISSING:
            self.denied.add(d.input_event_id)

    def _edit(self, event: ContextEditedEvent) -> None:
        for edit in event.data.edits:
            if edit.action == "clear":
                self.cleared.add(edit.call_id)
            elif edit.part is not MISSING and edit.spans is not MISSING:
                key = (edit.call_id, edit.part)
                self.redactions[key] = self.redactions.get(key, ()) + tuple(edit.spans)


def render_view(events: Sequence[Event]) -> RenderView:
    b = _Builder()
    for event in events:
        b.add(event)
    outer = tuple(c for c in b.compactions if not any(covers(o, c.seq) for o in b.compactions))
    return RenderView(
        frozenset(b.denied),
        frozenset(b.side),
        b.cut,
        outer,
        frozenset(b.cleared),
        dict(b.redactions),
        frozenset(c for c in b.calls if c not in b.proposed),
        frozenset(i for i, by in b.recalled if by != b.principal),
    )


def input_principal(events: Sequence[Event]) -> Principal | None:
    """The current input's principal: the latest `user_input` or `steer`'s."""
    inputs = (e for e in reversed(events) if isinstance(e, UserInputEvent | SteerEvent))
    return next((e.actor.principal for e in inputs), None)


def _host_result(view: RenderView, event: Event) -> bool:
    return (
        isinstance(event, ToolResultEvent | ToolResultLateEvent)
        and event.data.call_id in view.host_calls
    )


def covers(compacted: CompactedEvent, seq: int) -> bool:
    return compacted.data.from_seq <= seq <= compacted.data.to_seq


def assistant_parts(
    view: RenderView, event: ModelResponseEvent | ModelResponseRecoveredEvent
) -> tuple[OutputPart, ...]:
    """The parts a response renders: none for a compaction side response, and its reasoning
    and hosted-tool parts are omitted when it predates an omit_prior epoch."""
    if event.data.request_event_id in view.side:
        return ()
    opaque = (ReasoningPart, HostedToolPart) if event.seq < view.cut else ()
    return tuple(p for p in event.data.content if not isinstance(p, opaque))


def walk(view: RenderView, events: Sequence[Event]) -> Iterator[Event]:
    """The events in render order. A range's first position yields its `CompactedEvent` (the
    summary), then the last `tools_changed` inside the range, so loaded tools survive. A
    `compacted` event at its own position renders nothing and is not yielded."""
    for event in events:
        compacted = next((c for c in view.ranges if covers(c, event.seq)), None)
        if compacted is None:
            hidden = _host_result(view, event) or event.event_id in view.withheld
            if not isinstance(event, CompactedEvent) and not hidden:
                yield event
        elif event.seq == compacted.data.from_seq:
            yield compacted
            kept = [
                e for e in events if isinstance(e, ToolsChangedEvent) and covers(compacted, e.seq)
            ]
            if kept:
                yield kept[-1]
