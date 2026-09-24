"""Run completion (spec/schema/README.md, "Run completion"; Gate 1 decision 27): a run spans its
request's turn and every wake turn of the same request, and ends at the first point where no
turn is open, nothing is parked and every background child it spawned has reported. Only the
lead's own log decides it, so run(), the host's outcome and SSE, and replay agree. A turn opens
with a user_input, a woken (the run that spawned its children) or a received mail that opens a
turn (its provenance's root request). Reference: spec/tools/fixtures/run_end.py."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import JsonValue

from threads.log import (
    AgentFinishedEvent,
    AgentSpawnedEvent,
    CancelRequestedEvent,
    Event,
    EventId,
    MemberStartedEvent,
    MessageReceivedEvent,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    ParkAddress,
    ParkedEvent,
    ResumedEvent,
    ToolCallEvent,
    ToolResultLateEvent,
    ToolUsePart,
    TurnCompletedEvent,
    UserInputEvent,
    WaitStartedEvent,
    WokenEvent,
)
from threads.reduce.fold import Fold
from threads.reduce.team_fold import NOTICES, mail_renders, monitor_id

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


def ended_otherwise(status: RunStatus) -> bool:
    """A run that ended some other way than completed: it is never woken again."""
    return status in ("failed", "cancelled", "budget_exhausted", "handed_off")


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
    monitors: set[str] = field(default_factory=set[str])
    """The task monitors of this run's members that have not reported."""
    settle: set[str] = field(default_factory=set[str])
    """The settle monitors of this log's waits: their notifications open no turn."""
    tool_uses: dict[str, frozenset[str]] = field(default_factory=dict[str, frozenset[str]])
    """Each response's tool_use call ids, by request."""
    host_sends: set[str] = field(default_factory=set[str])
    """The effect keys of the host's own sends: a park on one is never the run's."""
    answered: tuple[Event, ...] | None = None
    decided: RunEnd | None = None

    def step(self, events: Sequence[Event], i: int) -> None:
        event = events[i]
        opens = None if self.open else self._opens(event)
        if opens is not None:
            self.open, self.start, self.current = True, i, opens[0]
        self._helpers(event)
        self._members(event)
        self._sends(event)
        if isinstance(event, CancelRequestedEvent):
            self._idle_cancel(event.data.scope, i)
        elif isinstance(event, TurnCompletedEvent):
            self._turn_end(events, i, event.data.reason)
        elif isinstance(event, ParkedEvent) and not self._host_park(event.data.address):
            self.parks.append(event.data.address)
        elif isinstance(event, ResumedEvent) and event.data.address in self.parks:
            self.parks.remove(event.data.address)
        # A late result and the woken naming it are one append: the end is never between them.
        mid_append = isinstance(event, AgentFinishedEvent | ToolResultLateEvent)
        if not mid_append and self.decided is None and self.ended():
            self.decided = RunEnd("completed", self.answered or (), i)

    def _opens(self, event: Event) -> tuple[EventId | None] | None:
        """The run of the turn `event` opens, or None when it opens none: an input, a woken (its
        children's run) or a received mail that opens a turn (its root request)."""
        if isinstance(event, UserInputEvent):
            return (event.event_id,)
        if isinstance(event, WokenEvent):
            return (self.late_runs.get(event.data.causes[0]),)
        if not isinstance(event, MessageReceivedEvent):
            return None
        env = event.data.envelope
        resolved = all(p.kind == "member" and p.id == env.monitor_id for p in self.parks)
        if not (mail_renders(env, self.settle) and resolved):
            return None
        return (env.provenance.root_request.event_id,)

    def _members(self, event: Event) -> None:
        """Run-owned members, by their task monitors, and the waits' settle monitors."""
        if isinstance(event, MemberStartedEvent):
            if event.data.provenance.root_request.event_id == self.request:
                self.monitors.add(monitor_id(event, "task"))
        elif isinstance(event, MessageReceivedEvent):
            env = event.data.envelope
            if env.kind in NOTICES and isinstance(env.monitor_id, str):
                self.monitors.discard(env.monitor_id)
        elif isinstance(event, WaitStartedEvent):
            self.settle.update(monitor_id(event, m.name) for m in event.data.members)

    def _sends(self, event: Event) -> None:
        """The host's own sends (reduce/fold.py host_calls): a channel_send no response asked
        for."""
        if isinstance(event, ModelResponseEvent | ModelResponseRecoveredEvent):
            ids = frozenset(
                str(p.call_id) for p in event.data.content if isinstance(p, ToolUsePart)
            )
            self.tool_uses[event.data.request_event_id] = ids
        elif isinstance(event, ToolCallEvent) and event.data.name == "channel_send":
            if event.data.call_id not in self.tool_uses.get(
                event.data.request_event_id, frozenset()
            ):
                self.host_sends.add(f"{event.branch_id}:{event.data.call_id}")

    def _host_park(self, address: ParkAddress) -> bool:
        return address.kind == "effect" and address.id in self.host_sends

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

    def _idle_cancel(self, scope: str, i: int) -> None:
        """A thread or tree cancel while no turn is open ends a run still waiting on its
        children."""
        waiting = self.answered is not None and not self.open
        if waiting and scope != "turn" and self.decided is None:
            self.decided = RunEnd("cancelled", (), i)

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
        idle = not (self.open or self.parks or self.children or self.monitors)
        return self.answered is not None and idle

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
