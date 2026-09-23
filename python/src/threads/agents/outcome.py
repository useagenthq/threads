"""A run's `RunResult`, read off the log it wrote: the turn's end reason decides the variant."""

from collections.abc import Sequence
from typing import assert_never

from threads.agents.results import (
    BudgetExhausted,
    Cancelled,
    Completed,
    Failed,
    Parked,
    RunError,
    RunResult,
    Thread,
)
from threads.log import (
    BudgetExceededEvent,
    Event,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    TextPart,
)
from threads.loop import runtime
from threads.loop.runtime import FAILED_CODES, Halt


def result(rt: runtime.Runtime, halt: Halt, thread: Thread) -> RunResult[str]:
    match halt:
        case runtime.Parked(reason=reason, pending=pending):
            return Parked(reason, pending, thread)
        case runtime.Failed(code=code, message=message):
            return Failed(RunError(code, message), thread)
        case runtime.Idle(reason=reason):
            return ended(rt.events, reason, thread)
        case _:
            assert_never(halt)


def ended(events: Sequence[Event], reason: str, thread: Thread) -> RunResult[str]:
    if reason == "cancelled":
        return Cancelled(thread)
    if reason == "budget_exhausted":
        exceeded = next(e for e in reversed(events) if isinstance(e, BudgetExceededEvent))
        return BudgetExhausted(exceeded.data, thread)
    code = FAILED_CODES.get(reason)
    if code is not None:
        return Failed(RunError(code, f"the turn ended {reason}"), thread)
    return Completed(final_text(events), thread)


def final_text(events: Sequence[Event]) -> str:
    """The text of the turn's last response: the output of an agent without an output type."""
    for event in reversed(events):
        if isinstance(event, ModelResponseEvent | ModelResponseRecoveredEvent):
            return "".join(p.text for p in event.data.content if isinstance(p, TextPart))
    return ""
