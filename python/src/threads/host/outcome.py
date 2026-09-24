"""A run's result as data (host-api `RunOutcome`), and read back from the log.

A run is named by its user_input's event_id. Its outcome is decided by run completion
(spec/schema/README.md, "Run completion"): the answer after its last wake once every background
child it spawned has reported, the first of its turns that ended otherwise, or an open park. So a
subscriber that arrives late, or after a restart, reads the same outcome the run returned.
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
from threads.loop.runtime import FAILED_MESSAGES
from threads.reduce.handlers import to_json
from threads.reduce.run_end import run_end
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
    end = run_end(events, events[start].event_id)
    if end.status == "running":
        return None
    if end.status == "parked":
        last = next(e for e in reversed(events) if isinstance(e, ParkedEvent))
        return outcome(Parked(last.data.reason, tuple(parked), thread))
    done = end.turn[-1]
    if not isinstance(done, TurnCompletedEvent):
        raise AssertionError("an ended run ended its turn")
    return _ended(end.turn, done, thread)


def run_events(events: Sequence[Event], start: int) -> Sequence[Event]:
    """The run's own events: through where it ended, else up to the next input. A stream never
    shows another run's events."""
    end = run_end(events, events[start].event_id)
    if end.at is not None:
        return events[start : end.at + 1]
    later = (i for i in range(start + 1, len(events)) if isinstance(events[i], UserInputEvent))
    return events[start : next(later, len(events))]


def _ended(events: Sequence[Event], end: TurnCompletedEvent, thread: Thread) -> JsonValue:
    ids: dict[str, JsonValue] = {"thread_id": thread.id, "branch_id": thread.branch}
    match end.data.reason:
        case "handoff":
            moved = next(e for e in reversed(events) if isinstance(e, HandoffEvent))
            return {"status": "handed_off", **ids, "to_thread_id": moved.data.to_thread_id}
        case "error" | "interrupted":
            code = end.data.code if isinstance(end.data.code, str) else "model_error"
            message = FAILED_MESSAGES[end.data.reason]
            return {"status": "failed", **ids, "error": {"code": code, "message": message}}
        case reason:
            return outcome(ended(events, reason, thread))
