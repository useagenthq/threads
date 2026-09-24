"""Turn spans: which trace a turn joins (spec/otel/README.md, "Roots and parents"), and the
bookkeeping of the turn that is open, the one a park closed, and the runs they belong to."""

from collections.abc import Callable
from dataclasses import dataclass, replace

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    Event,
    MessageReceivedEvent,
    Parent,
    ThreadStartedEvent,
    ToolResultLateEvent,
    TurnCompletedEvent,
    UserInputEvent,
    WokenEvent,
)
from threads.otel.attrs import span_event, turn_attrs, turn_status
from threads.otel.ids import span_id, trace_id
from threads.otel.span import INTERNAL, Context, Link, OpenSpan, Span

type ParentOf = Callable[[Parent], Link | None]
"""Where a child thread's first turn goes, as the parent chain says; None: missing."""


@dataclass(frozen=True, slots=True)
class Turn:
    """A turn span and the context its resumed turns inherit."""

    span: OpenSpan
    context: Context


class Turns:
    def __init__(self, parent_of: ParentOf) -> None:
        self.parent_of = parent_of
        self.agent = "agent"
        self.open: Turn | None = None
        self.parked: Turn | None = None
        """The turn a park closed, until the next event opens the resumed turn."""
        self.call_contexts: dict[str, Context] = {}
        """Each call's turn context, for a woken turn that its late result opens."""
        self._late_calls: dict[str, str] = {}
        self._started: ThreadStartedEvent | None = None
        self._turns = 0

    def see(self, e: Event) -> None:
        """Bookkeeping every event feeds, before any span opens at it."""
        if isinstance(e, ThreadStartedEvent):
            self._started = e
        elif isinstance(e, ToolResultLateEvent):
            self._late_calls[e.event_id] = e.data.call_id

    def begin(self, e: Event) -> Turn:
        """A turn opener's turn, in the trace its rules give."""
        context = self._first() or self._root_of(e)
        self._turns += 1
        # Only a user_input names its run; a woken or mail turn's run would be a guess.
        if isinstance(e, UserInputEvent):
            context = replace(context, run_id=e.event_id)
        return self._start(e, context, ())

    def resume(self, e: Event, parked: Turn) -> Turn:
        """The turn a park interrupted goes on at `e`, in its trace, linked to its span."""
        return self._start(e, parked.context, (parked.span.id,))

    def end(self, e: Event, branch_id: str, tenant: str) -> Span | None:
        """Closes the open turn at a park or a turn_completed."""
        turn = self.open
        if turn is None:
            return None
        self.open = None
        if isinstance(e, TurnCompletedEvent):
            turn.span.attrs["threads.turn.reason"] = e.data.reason
            return turn.span.close(e, branch_id, tenant, turn_status(e.data.reason))
        turn.span.attrs["threads.parked"] = True
        self.parked = turn
        return turn.span.close(e, branch_id, tenant, None)

    def note(self, e: Event, retry_of: str | None = None) -> None:
        """Attaches a span event to the open turn, if there is one."""
        if self.open is not None:
            self.open.span.events.append(span_event(e, retry_of))

    def _start(self, e: Event, context: Context, links: tuple[Link, ...]) -> Turn:
        attrs = turn_attrs(self.agent, e.thread_id, context.run_id, context.parent_missing)
        span = OpenSpan(
            Link(context.trace_id, span_id(e.branch_id, e.event_id)),
            context.parent_span_id,
            f"invoke_agent {self.agent}",
            INTERNAL,
            e,
            attrs,
            links,
        )
        self.open = Turn(span, context)
        self.parked = None
        return self.open

    def _parent(self) -> Parent | None:
        """The parent a child thread's first turn is under (not a team member's)."""
        if self._turns > 0 or self._started is None:
            return None
        parent = self._started.data.parent
        if parent is MISSING or parent.relation == "team_member":
            return None
        return parent

    def _first(self) -> Context | None:
        """A child thread's first turn: under the parent's span, if it is still there."""
        parent = self._parent()
        anchor = None if parent is None else self.parent_of(parent)
        return None if anchor is None else Context(anchor.trace_id, anchor.span_id)

    def _root_of(self, e: Event) -> Context:
        missing = self._parent() is not None
        if isinstance(e, WokenEvent):
            call = self._late_calls.get(e.data.causes[0])
            joined = None if call is None else self.call_contexts.get(call)
            if joined is not None:
                return Context(joined.trace_id, None, missing)
        if isinstance(e, MessageReceivedEvent):
            root = e.data.envelope.provenance.root_request
            return Context(trace_id(root.thread_id, root.event_id), None, missing)
        return Context(trace_id(e.thread_id, e.event_id), None, missing)
