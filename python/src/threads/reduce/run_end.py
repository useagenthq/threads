"""Run completion (spec/schema/README.md, "Run completion"; Gate 1 decision 27): a run spans its
request's turn and every wake turn of the same request, and ends at the first point where no
turn is open, nothing is parked and every background child it spawned has reported. Only the
lead's own log decides it, so run(), the host's outcome and SSE, and replay agree. Team events
are refused before the Teams build, so the openers here are user_input and woken."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import JsonValue

from threads.log import (
    AgentFinishedEvent,
    AgentSpawnedEvent,
    Event,
    EventId,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    ParkAddress,
    ParkedEvent,
    ResumedEvent,
    ToolResultLateEvent,
    TurnCompletedEvent,
    UserInputEvent,
    WokenEvent,
)
from threads.reduce.fold import Fold

type RunStatus = Literal[
    "running", "parked", "completed", "failed", "cancelled", "budget_exhausted", "handed_off"
]


@dataclass(frozen=True, slots=True)
class RunEnd:
    status: RunStatus
    turn: tuple[Event, ...] = ()
    """The turn that decided it: the last answered one when completed, else the one that ended
    otherwise. Empty while running or parked."""
    at: int | None = None
    """Where in the events the run ended; None while running or parked."""


def _ended_as(reason: str) -> RunStatus:
    """A turn_completed reason other than end_turn: the status it ends the run with."""
    if reason == "cancelled":
        return "cancelled"
    if reason == "budget_exhausted":
        return "budget_exhausted"
    return "handed_off" if reason == "handoff" else "failed"


@dataclass(slots=True)
class _Run:
    request: EventId
    current: EventId | None = None
    open: bool = False
    start: int = 0
    parks: list[ParkAddress] = field(default_factory=list[ParkAddress])
    spawned: dict[str, EventId] = field(default_factory=dict[str, EventId])
    """The run that spawned each background call, by call id."""
    late_runs: dict[EventId, EventId] = field(default_factory=dict[EventId, EventId])
    children: set[str] = field(default_factory=set[str])
    """This run's background children that have not reported."""
    answered: tuple[Event, ...] | None = None
    decided: RunEnd | None = None

    def step(self, events: Sequence[Event], i: int) -> None:
        event = events[i]
        if not self.open and isinstance(event, UserInputEvent | WokenEvent):
            self.open, self.start = True, i
            self.current = (
                event.event_id
                if isinstance(event, UserInputEvent)
                else self.late_runs.get(event.data.causes[0])
            )
        self._helpers(event)
        if isinstance(event, TurnCompletedEvent):
            self._turn_end(events, i, event.data.reason)
        elif isinstance(event, ParkedEvent):
            self.parks.append(event.data.address)
        elif isinstance(event, ResumedEvent) and event.data.address in self.parks:
            self.parks.remove(event.data.address)
        # A late result and the woken naming it are one append: the end is never between them.
        mid_append = isinstance(event, AgentFinishedEvent | ToolResultLateEvent)
        if not mid_append and self.decided is None and self.ended():
            self.decided = RunEnd("completed", self.answered or (), i)

    def _helpers(self, event: Event) -> None:
        if isinstance(event, AgentSpawnedEvent) and event.data.mode == "background":
            if self.current is None:
                return
            self.spawned[event.data.call_id] = self.current
            if self.current == self.request:
                self.children.add(event.data.child_thread_id)
        elif isinstance(event, AgentFinishedEvent):
            self.children.discard(event.data.child_thread_id)
        elif isinstance(event, ToolResultLateEvent) and event.data.call_id in self.spawned:
            self.late_runs[event.event_id] = self.spawned[event.data.call_id]

    def _turn_end(self, events: Sequence[Event], i: int, reason: str) -> None:
        mine = self.open and self.current == self.request
        self.open = False
        if not mine or self.decided is not None:
            return
        turn = tuple(events[self.start : i + 1])
        if reason == "end_turn":
            self.answered = turn
        else:
            self.decided = RunEnd(_ended_as(reason), turn, i)

    def ended(self) -> bool:
        """An answer, no turn open, nothing parked, every child of this run reported."""
        return self.answered is not None and not self.open and not self.parks and not self.children

    def unfinished(self, last: int) -> RunEnd:
        """Where the log leaves a run that has not ended."""
        if self.ended():
            return RunEnd("completed", self.answered or (), last)
        # A park wins over an open turn: a lead parked mid-turn has returned parked.
        return RunEnd("parked" if self.parks else "running")


def run_end(events: Sequence[Event], request: EventId) -> RunEnd:
    """How the run named by its user_input `request` ended, from the log alone."""
    run = _Run(request)
    for i in range(len(events)):
        run.step(events, i)
        if run.decided is not None:
            return run.decided
    return run.unfinished(len(events) - 1)


def projection(fold: Fold) -> JsonValue:
    """The conformance `run` projection: the log's last run. None for a log with no input."""
    request = next((e for e in reversed(fold.events) if isinstance(e, UserInputEvent)), None)
    if request is None:
        return None
    end = run_end(fold.events, request.event_id)
    answers = [
        e for e in end.turn if isinstance(e, ModelResponseEvent | ModelResponseRecoveredEvent)
    ]
    output = answers[-1].event_id if end.status == "completed" and answers else None
    return {"status": end.status, "output_event_id": output}
