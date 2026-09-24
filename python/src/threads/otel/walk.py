"""One pass over a resolved chain in seq order: every span that closes on it, and the span open
at each agent_spawned and handoff (a child thread's parent). spec/otel/README.md, "Spans"."""

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from threads.log import (
    AgentSpawnedEvent,
    ApprovalDeniedEvent,
    ApprovalGrantedEvent,
    ApprovalRequestedEvent,
    BudgetExceededEvent,
    CompactedEvent,
    CompactionFailedEvent,
    EffectBeginEvent,
    EffectCommitEvent,
    EffectResolvedEvent,
    EffectUnknownEvent,
    Event,
    HandoffEvent,
    ModelAttemptAbandonedEvent,
    ModelRequestEvent,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    ParkedEvent,
    PermissionDecisionEvent,
    ResumedEvent,
    RetryScheduledEvent,
    SettingsChangedEvent,
    ThreadStartedEvent,
    ToolCallEvent,
    ToolResultEvent,
    ToolResultLateEvent,
    ToolsChangedEvent,
    ToolSpec,
    TurnCompletedEvent,
)
from threads.otel.calls import Calls
from threads.otel.models import Models
from threads.otel.span import Link, Span
from threads.otel.turns import ParentOf, Turns

_CALL_EVENTS = (
    PermissionDecisionEvent
    | ApprovalRequestedEvent
    | ApprovalGrantedEvent
    | ApprovalDeniedEvent
    | EffectBeginEvent
    | EffectCommitEvent
    | EffectUnknownEvent
    | EffectResolvedEvent
)
_TURN_EVENTS = ToolResultLateEvent | CompactedEvent | CompactionFailedEvent | BudgetExceededEvent
_MODEL_CLOSE = ModelResponseEvent | ModelResponseRecoveredEvent | ModelAttemptAbandonedEvent


@dataclass(frozen=True, slots=True)
class Walked:
    spans: tuple[Span, ...]
    """Every closed span, the parent prefix's included, in close order."""
    anchors: dict[str, Link]
    """The span open at each agent_spawned (its call's) and handoff (its turn), by event id."""


@dataclass(frozen=True, slots=True)
class WalkInput:
    tenant: str
    branch_id: str
    """The branch being exported; it names every span's threads.branch_id."""
    events: Sequence[Event]
    openers: Collection[str]
    """The event ids at which the reducer opens a turn."""
    content: bool
    parent_of: ParentOf


def walk(given: WalkInput) -> Walked:
    return _Walk(given).run()


class _Walk:
    def __init__(self, given: WalkInput) -> None:
        self.given = given
        self.spans: list[Span] = []
        self.anchors: dict[str, Link] = {}
        self.calls = Calls(given.content)
        self.models = Models(given.content)
        self.turns = Turns(given.parent_of)

    def run(self) -> Walked:
        for e in self.given.events:
            self._config(e)
            self.turns.see(e)
            self.calls.see(e)
            self._open(e)
            self._apply(e)
        return Walked(tuple(self.spans), self.anchors)

    def _config(self, e: Event) -> None:
        if isinstance(e, ThreadStartedEvent):
            self.turns.agent = e.data.agent_name
            self.models.model = e.data.model
            self._tools(e.data.tools)
        elif isinstance(e, ToolsChangedEvent):
            self._tools(e.data.tools)
        elif isinstance(e, SettingsChangedEvent):
            self.models.model = e.data.settings.model

    def _tools(self, tools: Sequence[ToolSpec]) -> None:
        self.calls.effect_classes = {t.name: t.effect_class for t in tools}

    def _open(self, e: Event) -> None:
        """A turn opener opens a turn; the first other event after a park opens the resumed one."""
        if e.event_id in self.given.openers:
            self.turns.begin(e)
            return
        parked = self.turns.parked
        if parked is not None and self.turns.open is None and not isinstance(e, ParkedEvent):
            self.turns.resume(e, parked)

    def _apply(self, e: Event) -> None:
        turn = self.turns.open
        if turn is None:
            return
        branch, tenant = self.given.branch_id, self.given.tenant
        if isinstance(e, ModelRequestEvent):
            self.models.request(e, turn)
        elif isinstance(e, _MODEL_CLOSE):
            self._close(self.models.close(e, branch, tenant))
        elif isinstance(e, RetryScheduledEvent):
            self.models.retry(e)
        elif isinstance(e, ToolCallEvent):
            self.turns.call_contexts[e.data.call_id] = turn.context
            self.calls.call(e, turn)
        elif isinstance(e, ToolResultEvent):
            self._close(self.calls.result(e, branch, tenant))
        elif isinstance(e, ResumedEvent):
            self.calls.resume(e, turn)
        elif isinstance(e, ParkedEvent | TurnCompletedEvent):
            self._end(e)
        else:
            self._note(e)

    def _note(self, e: Event) -> None:
        """Span events, and the anchors a child thread's first turn is parented to."""
        turn = self.turns.open
        if turn is None:
            return
        if isinstance(e, AgentSpawnedEvent):
            span = self.calls.open.get(e.data.call_id)
            if span is not None:
                self.anchors[e.event_id] = span.id
        elif isinstance(e, HandoffEvent):
            self.anchors[e.event_id] = turn.span.id
        elif isinstance(e, _CALL_EVENTS):
            if not self.calls.note(e.data.call_id, e):
                self.turns.note(e)
        elif isinstance(e, _TURN_EVENTS):
            self.turns.note(e)

    def _end(self, e: Event) -> None:
        """A park or turn_completed closes the turn's model and call spans, then the turn."""
        turn = self.turns.open
        if turn is None:
            return
        branch, tenant = self.given.branch_id, self.given.tenant
        self.spans.extend(self.models.end_all(e, turn, branch, tenant))
        self.spans.extend(self.calls.end_all(e, branch, tenant))
        self._close(self.turns.end(e, branch, tenant))

    def _close(self, span: Span | None) -> None:
        if span is not None:
            self.spans.append(span)
