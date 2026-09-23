"""What a run returns and streams (spec/api.json `RunResult`, `RunStream`; host-api StreamEvent).

Results are values: a run that parks, fails or runs out of budget returns that, and only a bug
or a `ConfigError` raises.
"""

from dataclasses import dataclass
from typing import Literal

from threads.log import (
    BudgetExceededData,
    EventId,
    ParkAddress,
)
from threads.loop.runtime import RunErrorCode
from threads.store import StoredEvent
from threads.thread.handle import Thread


@dataclass(frozen=True, slots=True)
class Completed[O]:
    output: O
    thread: Thread
    status: Literal["completed"] = "completed"


@dataclass(frozen=True, slots=True)
class Parked:
    reason: Literal["awaiting_approval", "effect_unknown", "awaiting_input", "awaiting_resource"]
    pending: tuple[ParkAddress, ...]
    thread: Thread
    status: Literal["parked"] = "parked"


@dataclass(frozen=True, slots=True)
class Cancelled:
    thread: Thread
    status: Literal["cancelled"] = "cancelled"


@dataclass(frozen=True, slots=True)
class RunError:
    code: RunErrorCode
    message: str


@dataclass(frozen=True, slots=True)
class Failed:
    error: RunError
    thread: Thread
    status: Literal["failed"] = "failed"


@dataclass(frozen=True, slots=True)
class BudgetExhausted:
    budget: BudgetExceededData
    thread: Thread
    status: Literal["budget_exhausted"] = "budget_exhausted"


@dataclass(frozen=True, slots=True)
class HandedOff:
    thread: Thread
    to_thread: Thread
    status: Literal["handed_off"] = "handed_off"


type RunResult[O] = Completed[O] | Parked | Cancelled | Failed | BudgetExhausted | HandedOff


@dataclass(frozen=True, slots=True)
class EventItem:
    """A committed log event: durable."""

    event: StoredEvent
    kind: Literal["event"] = "event"


@dataclass(frozen=True, slots=True)
class DeltaItem:
    """Streamed text of an attempt in flight: transient, never logged."""

    request_event_id: EventId
    text: str
    kind: Literal["delta"] = "delta"


@dataclass(frozen=True, slots=True)
class StatusItem:
    """A recorded retry wait in progress: transient, never logged."""

    until: int
    status: Literal["retry_wait"] = "retry_wait"
    kind: Literal["status"] = "status"


type StreamEvent = EventItem | DeltaItem | StatusItem
