"""A run's `RunResult`, read off the log it wrote (spec/schema/README.md, "Run completion"): the
reason its deciding turn ended decides the variant."""

from collections.abc import Sequence
from typing import assert_never

from pydantic.experimental.missing_sentinel import MISSING

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
    OutputValidatedEvent,
    TextPart,
    TurnCompletedEvent,
    UserInputEvent,
)
from threads.log.jcs import canonicalize
from threads.loop import runtime
from threads.loop.history import turn_events
from threads.loop.runtime import FAILED_CODES, FAILED_MESSAGES, Halt, RunErrorCode
from threads.reduce.run_end import run_end
from threads.result import Ok


def result(rt: runtime.Runtime, halt: Halt, thread: Thread) -> RunResult[str]:
    match halt:
        case runtime.Parked(reason=reason, pending=pending):
            return Parked(reason, pending, thread)
        case runtime.Failed(code=code, message=message):
            return Failed(RunError(code, message), thread)
        case runtime.Idle():
            return _run_result(rt.events, thread)
        case _:
            assert_never(halt)


def _run_result(events: Sequence[Event], thread: Thread) -> RunResult[str]:
    """The ended run of the latest input: completed with the answer after its last wake, or
    the outcome of its turn that ended otherwise."""
    request = next(e for e in reversed(events) if isinstance(e, UserInputEvent))
    end = run_end(events, request.event_id)
    if end.status == "cancelled" and not end.turn:
        # A cancel while the run waited on its children ends it outside any turn.
        return Cancelled(thread)
    turn = end.turn
    done = turn[-1] if turn else None
    if not isinstance(done, TurnCompletedEvent):
        raise AssertionError("an idle run ended its turn")
    return ended(turn, done.data.reason, thread)


def ended(events: Sequence[Event], reason: str, thread: Thread) -> RunResult[str]:
    if reason == "cancelled":
        return Cancelled(thread)
    if reason == "budget_exhausted":
        exceeded = next(e for e in reversed(events) if isinstance(e, BudgetExceededEvent))
        return BudgetExhausted(exceeded.data, thread)
    code = _turn_code(events) if reason == "error" else None
    code = code or FAILED_CODES.get(reason)
    if code is not None:
        return Failed(RunError(code, FAILED_MESSAGES[reason]), thread)
    return Completed(output_text(events), thread)


def _turn_code(events: Sequence[Event]) -> RunErrorCode | None:
    """The typed code an error turn ended with (turn_completed.code), when it has one."""
    ended = next((e for e in reversed(events) if isinstance(e, TurnCompletedEvent)), None)
    if ended is None or ended.data.code is MISSING:
        return None
    code = ended.data.code
    # Only a team member's rebind ends a turn this way, and a member's result is a MemberResult.
    if code in ("pin_unavailable", "pin_mismatch", "setup_failed"):
        raise AssertionError(f"a run's turn can't end {code}: only a member rebinds")
    return code


def output_text(events: Sequence[Event]) -> str:
    """The run's output as text: the turn's accepted final_output value as canonical JSON
    (what a parent sees of a structured subagent, and what `Agent.run` parses into its output
    type), else the text of the turn's last response."""
    turn = turn_events(events)
    accepted = next(
        (
            e
            for e in reversed(turn)
            if isinstance(e, OutputValidatedEvent) and e.data.outcome == "accepted"
        ),
        None,
    )
    if accepted is not None and accepted.data.value is not MISSING:
        text = canonicalize(accepted.data.value)
        if not isinstance(text, Ok):
            raise AssertionError("a recorded value always canonicalizes")
        return text.value
    for event in reversed(events):
        if isinstance(event, ModelResponseEvent | ModelResponseRecoveredEvent):
            return "".join(p.text for p in event.data.content if isinstance(p, TextPart))
    return ""
