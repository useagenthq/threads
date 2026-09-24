"""What a run's frames read from the events before one: the purpose of each model request,
whether a model step is open, the legacy subagents it started, its tool calls, approval
challenges and park reasons. Fed one committed event at a time, in log order."""

from dataclasses import dataclass

from threads.log import (
    AgentFinishedEvent,
    AgentSpawnedEvent,
    ApprovalRequestedEvent,
    Event,
    JsonObject,
    ModelAttemptAbandonedEvent,
    ModelRequestEvent,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    ParkAddress,
    ParkedEvent,
    ToolCallEvent,
    ToolUsePart,
)


@dataclass(frozen=True, slots=True)
class Spawned:
    child: str
    agent: str
    call_id: str


@dataclass(frozen=True, slots=True)
class Challenge:
    call_id: str
    expires_at: int


@dataclass(frozen=True, slots=True)
class Call:
    name: str
    input: JsonObject


def _key(a: ParkAddress) -> str:
    return f"{a.kind}:{a.id}"


class RunFacts:
    def __init__(self) -> None:
        self._compaction: set[str] = set()
        self._answered: set[str] = set()
        self._shown: set[str] = set()
        """Calls a turn response of this run proposed: a result shows only for one of these."""
        self._last_turn: str | None = None
        self._last_stop: str | None = None
        self._spawned: dict[str, Spawned] = {}
        self._finished: set[str] = set()
        self._calls: dict[str, Call] = {}
        self._challenges: dict[str, Challenge] = {}
        self._parks: dict[str, str] = {}

    def add(self, e: Event) -> None:
        match e:
            case ModelRequestEvent() | ModelResponseEvent() | ModelResponseRecoveredEvent():
                self._model(e)
            case ModelAttemptAbandonedEvent(data=data):
                self._answered.add(data.request_event_id)
            case AgentSpawnedEvent(data=data):
                self._spawned[data.child_thread_id] = Spawned(
                    data.child_thread_id, data.agent_name, data.call_id
                )
            case AgentFinishedEvent(data=data):
                self._finished.add(data.child_thread_id)
            case ToolCallEvent(data=data):
                self._calls[data.call_id] = Call(data.name, data.input)
            case ApprovalRequestedEvent(data=data):
                self._challenges[data.challenge_id] = Challenge(data.call_id, data.expires_at)
            case ParkedEvent(data=data):
                self._parks[_key(data.address)] = data.reason
            case _:
                pass

    def _model(
        self, e: ModelRequestEvent | ModelResponseEvent | ModelResponseRecoveredEvent
    ) -> None:
        if isinstance(e, ModelRequestEvent):
            if e.data.purpose == "compaction":
                self._compaction.add(e.event_id)
            else:
                self._last_turn = e.event_id
            return
        request = e.data.request_event_id
        self._answered.add(request)
        if self.is_turn(request):
            self._last_stop = e.data.stop_reason
            self._shown |= {p.call_id for p in e.data.content if isinstance(p, ToolUsePart)}

    def is_turn(self, request_id: str) -> bool:
        """A compaction side request's frames are none; an unknown request counts as a turn's."""
        return request_id not in self._compaction

    def open_step(self) -> str | None:
        """The turn request with no response and no abandonment yet, if the last one is open."""
        last = self._last_turn
        return last if last is not None and last not in self._answered else None

    def last_stop(self) -> str | None:
        return self._last_stop

    def proposed(self, call_id: str) -> bool:
        return call_id in self._shown

    def spawned(self, child: str) -> Spawned | None:
        return self._spawned.get(child)

    def running(self) -> list[Spawned]:
        """Legacy subagents started in this run and not finished, in start order."""
        return [s for s in self._spawned.values() if s.child not in self._finished]

    def call(self, call_id: str) -> Call | None:
        return self._calls.get(call_id)

    def challenge(self, challenge_id: str) -> Challenge | None:
        return self._challenges.get(challenge_id)

    def park_reason(self, address: ParkAddress) -> str | None:
        return self._parks.get(_key(address))
