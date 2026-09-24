"""Tool spans and their continuations (spec/otel/README.md): one open span per call at most,
the calls still pending, and which of them can run when a parked turn resumes."""

from typing import TYPE_CHECKING

from threads.log import (
    ApprovalDeniedEvent,
    ApprovalGrantedEvent,
    ApprovalRequestedEvent,
    Event,
    ParkedEvent,
    PermissionDecisionEvent,
    ResumedEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from threads.otel.attrs import span_event, tool_attrs
from threads.otel.ids import span_id
from threads.otel.span import INTERNAL, Link, OpenSpan, Span
from threads.otel.turns import Turn

if TYPE_CHECKING:
    from collections.abc import Mapping


class Calls:
    def __init__(self, content: bool) -> None:
        self.content = content
        self.open: dict[str, OpenSpan] = {}
        """The open tool or continuation span of each call."""
        self.effect_classes: Mapping[str, str] = {}
        """Effect classes of the latest pinned tool set, by tool name."""
        self._pending: dict[str, ToolCallEvent] = {}
        """Calls with a tool_call and no tool_result yet, in call order."""
        self._awaiting: set[str] = set()
        """Calls asked about (permission ask, approval requested) and not answered yet."""
        self._denied: set[str] = set()
        self._previous: dict[str, Link] = {}
        """Each call's latest span, which its next continuation links to."""

    def see(self, e: Event) -> None:
        """Tracks what decides which calls a resumed turn continues."""
        if isinstance(e, ToolCallEvent):
            self._pending[e.data.call_id] = e
        elif isinstance(e, ToolResultEvent):
            self._pending.pop(e.data.call_id, None)
        elif isinstance(e, ApprovalRequestedEvent) or (
            isinstance(e, PermissionDecisionEvent) and e.data.decision == "ask"
        ):
            self._awaiting.add(e.data.call_id)
        elif isinstance(e, ApprovalGrantedEvent):
            self._awaiting.discard(e.data.call_id)
        elif isinstance(e, ApprovalDeniedEvent):
            self._awaiting.discard(e.data.call_id)
            self._denied.add(e.data.call_id)

    def call(self, e: ToolCallEvent, turn: Turn) -> None:
        """The tool span a tool_call opens under its turn."""
        self._start(e, e, turn, span_id(e.branch_id, e.event_id), resumed=False)

    def resume(self, e: ResumedEvent, turn: Turn) -> None:
        """At a resumed: a continuation of every pending call that can run now."""
        for call_id, call in self._pending.items():
            waiting = call_id in self._awaiting or call_id in self._denied
            if waiting or call_id in self.open:
                continue
            previous = self._previous.get(call_id)
            links = () if previous is None else (previous,)
            sid = span_id(e.branch_id, e.event_id, call_id)
            self._start(e, call, turn, sid, resumed=True, links=links)

    def note(self, call_id: str, e: Event) -> bool:
        """Attaches a call's event to its open span; False when it has none."""
        span = self.open.get(call_id)
        if span is not None:
            span.events.append(span_event(e))
        return span is not None

    def result(self, e: ToolResultEvent, branch_id: str, tenant: str) -> Span | None:
        """The call's result closes its span, with status ERROR for an error result."""
        span = self.open.pop(e.data.call_id, None)
        if span is None:
            return None
        if self.content:
            span.attrs["gen_ai.tool.call.result"] = e.data.preview
        return span.close(e, branch_id, tenant, e.data.origin if e.data.is_error else None)

    def end_all(self, e: Event, branch_id: str, tenant: str) -> list[Span]:
        """A turn's close closes every call span still open: parked by a park, else cut."""
        closed: list[Span] = []
        for span in self.open.values():
            if isinstance(e, ParkedEvent):
                span.attrs["threads.parked"] = True
                closed.append(span.close(e, branch_id, tenant, None))
            else:
                span.attrs["threads.cut"] = True
                closed.append(span.close(e, branch_id, tenant, "cut"))
        self.open.clear()
        return closed

    def _start(  # noqa: PLR0913 - one span's parts
        self,
        opener: Event,
        call: ToolCallEvent,
        turn: Turn,
        sid: str,
        *,
        resumed: bool,
        links: tuple[Link, ...] = (),
    ) -> None:
        effect_class = self.effect_classes.get(call.data.name)
        attrs = tool_attrs(call, effect_class, resumed, self.content)
        link = Link(turn.context.trace_id, sid)
        span = OpenSpan(
            link,
            turn.span.id.span_id,
            f"execute_tool {call.data.name}",
            INTERNAL,
            opener,
            attrs,
            links,
        )
        self.open[call.data.call_id] = span
        self._previous[call.data.call_id] = link
