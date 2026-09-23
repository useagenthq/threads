"""A run's result as data (host-api `RunOutcome`), and read back from the log.

A run is named by its user_input's event_id. Its outcome is decided by the first turn end after
that input, or, while none has come, by an open park. So a subscriber that arrives late, or
after a restart, reads the same outcome the run returned.
"""

from collections.abc import Sequence
from typing import assert_never

from pydantic import JsonValue

from threads.agents.outcome import ended
from threads.agents.results import (
    BudgetExhausted,
    Cancelled,
    Completed,
    Failed,
    HandedOff,
    Parked,
    RunResult,
)
from threads.log import (
    Event,
    EventId,
    HandoffEvent,
    ParkAddress,
    ParkedEvent,
    TurnCompletedEvent,
    UserInputEvent,
)
from threads.reduce.handlers import to_json
from threads.thread.handle import Thread


def outcome(result: RunResult[str]) -> JsonValue:
    """RunResult as the HTTP form: ids where the library carries a Thread handle."""
    ids: dict[str, JsonValue] = {"thread_id": result.thread.id, "branch_id": result.thread.branch}
    match result:
        case Completed(output=output):
            return {"status": "completed", **ids, "output": output}
        case Parked(reason=reason, pending=pending):
            addresses: list[JsonValue] = [to_json(a) for a in pending]
            return {"status": "parked", **ids, "reason": reason, "pending": addresses}
        case Cancelled():
            return {"status": "cancelled", **ids}
        case Failed(error=error):
            return {
                "status": "failed",
                **ids,
                "error": {"code": error.code, "message": error.message},
            }
        case BudgetExhausted(budget=budget):
            return {"status": "budget_exhausted", **ids, "budget": to_json(budget)}
        case HandedOff(to_thread=to):
            return {"status": "handed_off", **ids, "to_thread_id": to.id}
        case _:
            assert_never(result)


def run_start(events: Sequence[Event], run_id: EventId) -> int | None:
    """The run's user_input position, or None when this branch has no such run."""
    return next(
        (i for i, e in enumerate(events) if isinstance(e, UserInputEvent) and e.event_id == run_id),
        None,
    )


def logged(
    events: Sequence[Event], start: int, parked: Sequence[ParkAddress], thread: Thread
) -> JsonValue | None:
    """The outcome the log records for the run starting at `start`, or None while it runs."""
    rest = events[start + 1 :]
    for i, event in enumerate(rest):
        if isinstance(event, TurnCompletedEvent):
            return _ended(rest[: i + 1], event, thread)
    if parked and any(isinstance(e, ParkedEvent) for e in rest):
        last = next(e for e in reversed(rest) if isinstance(e, ParkedEvent))
        return outcome(Parked(last.data.reason, tuple(parked), thread))
    return None


def _ended(events: Sequence[Event], end: TurnCompletedEvent, thread: Thread) -> JsonValue:
    ids: dict[str, JsonValue] = {"thread_id": thread.id, "branch_id": thread.branch}
    match end.data.reason:
        case "handoff":
            moved = next(e for e in reversed(events) if isinstance(e, HandoffEvent))
            return {"status": "handed_off", **ids, "to_thread_id": moved.data.to_thread_id}
        case "error" | "interrupted":
            code = end.data.code if isinstance(end.data.code, str) else "model_error"
            message = f"the turn ended {end.data.reason}"
            return {"status": "failed", **ids, "error": {"code": code, "message": message}}
        case reason:
            return outcome(ended(events, reason, thread))
