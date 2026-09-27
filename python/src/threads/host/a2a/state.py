"""A committed run slice as an A2A `Task`. A pure function of the events: the same log always
renders the same task, which is what lets a stream resume, a GetTask after a stream and two hosts
all answer the same thing.

The outcome comes from the host's own run-slice reader (`threads.host.outcome.logged`), so there
is no second reducer to keep in step. That reader answers the host-api `RunOutcome` shape, which
is JSON, and this file reads it as JSON rather than keeping a second typed copy of its variants."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import JsonValue

from threads._generated.a2a_v1 import Task, TaskState
from threads.host.a2a.keys import artifact_id, status_message_id
from threads.host.a2a.question import OpenAsk, ask_text
from threads.log import Event, EventId, ModelRequestEvent


@dataclass(frozen=True, slots=True)
class Slice:
    """One state a run passed through, as the log has it."""

    task_id: EventId
    context_id: str
    own: Sequence[Event]
    """The run's own events, from its user_input to where the run ends or the log stops."""
    outcome: JsonValue
    """`threads.host.outcome.logged` over the same prefix; None while the run is still going."""
    question: OpenAsk | None
    """The question open at the end of this prefix, so a replayed frame shows the right one."""


def task_of(view: Slice) -> Task:
    """The slice as the task a partner sees. Built as JSON and parsed, because an absent A2A field
    is absent rather than null: a boundary model refuses the MISSING sentinel as input."""
    state, text = _state_of(view)
    last = view.own[-1] if view.own else None
    status: dict[str, JsonValue] = {"state": state}
    if text is not None:
        status["message"] = _status_message(view, 0 if last is None else last.seq, text)
    if last is not None:
        status["timestamp"] = _rfc3339(last.time)
    built: dict[str, JsonValue] = {
        "id": view.task_id,
        "contextId": view.context_id,
        "status": status,
    }
    artifacts = artifacts_of(view)
    if artifacts is not None:
        built["artifacts"] = list(artifacts)
    return Task.model_validate(built)


def _state_of(view: Slice) -> tuple[TaskState, str | None]:
    """The task's state, and the status message's text when the state carries one."""
    outcome = view.outcome
    if outcome is None:
        # A run whose input is recorded but which has not asked the model anything yet has been
        # accepted and nothing more: submitted, not working.
        return ("TASK_STATE_WORKING" if _ran(view) else "TASK_STATE_SUBMITTED", None)
    status = _text(_at(outcome, "status"))
    plain = _SETTLED.get(status)
    if plain is not None:
        return (plain, None)
    if status == "parked":
        return _parked(view, _text(_at(outcome, "reason")))
    return _ended(view, _badly(outcome, status))


_SETTLED: dict[str, TaskState] = {
    "completed": "TASK_STATE_COMPLETED",
    "cancelled": "TASK_STATE_CANCELED",
}
"""The two endings that need no explaining: there is nothing a status message would add."""


def _badly(outcome: JsonValue, status: str) -> str:
    """Why a run ended badly, as the status message words it."""
    if status == "failed":
        error = _at(outcome, "error")
        return f"{_text(_at(error, 'code'))}: {_text(_at(error, 'message'))}"
    if status == "budget_exhausted":
        return _budget(_at(outcome, "budget"))
    # An agent with handoffs cannot be exposed, so handed_off is unreachable by construction.
    return f"{status}: an exposed agent cannot hand off"


def _budget(budget: JsonValue) -> str:
    scope, limit = _text(_at(budget, "scope")), _text(_at(budget, "limit"))
    return (
        f"budget_exhausted: {scope} budget {limit} of {_number(_at(budget, 'limit_value'))} reached"
    )


def _ended(view: Slice, text: str) -> tuple[TaskState, str]:
    """A run that ended badly: failed after it ran, rejected when it never got as far as a model
    request (a policy refusal, the host ceiling, or a budget already spent at the start)."""
    return ("TASK_STATE_FAILED" if _ran(view) else "TASK_STATE_REJECTED", text)


def _ran(view: Slice) -> bool:
    return any(isinstance(e, ModelRequestEvent) for e in view.own)


_PARKS: dict[str, tuple[TaskState, str]] = {
    "awaiting_approval": ("TASK_STATE_WORKING", "waiting for approval"),
    "effect_unknown": (
        "TASK_STATE_WORKING",
        "waiting for a person to resolve an uncertain action",
    ),
}
"""A park as a caller sees it. An approval and an uncertain effect show WORKING on purpose, because
neither can be decided over A2A and neither means the work failed: we do not know, so we say
working, and a person resolves it through threads."""


def _parked(view: Slice, reason: str) -> tuple[TaskState, str]:
    if reason == "awaiting_input":
        question = view.question
        return (
            "TASK_STATE_INPUT_REQUIRED",
            "a question is open" if question is None else ask_text(question.ask),
        )
    # Waiting on something inside this host, which a caller can neither see nor resolve: working is
    # the honest answer, and the reason is the most we can say without leaking what it is.
    return _PARKS.get(reason, ("TASK_STATE_WORKING", reason))


def _status_message(view: Slice, seq: int, text: str) -> JsonValue:
    """Role agent, the task's own ids, a derived id and one text part."""
    return {
        "messageId": status_message_id(view.task_id, seq),
        "contextId": view.context_id,
        "taskId": view.task_id,
        "role": "ROLE_AGENT",
        "parts": [{"text": text}],
    }


def artifacts_of(view: Slice) -> tuple[JsonValue, ...] | None:
    """A completed run's one artifact: a text part, or a data part when the agent has an output
    schema, which is exactly when its output is not a bare string."""
    outcome = view.outcome
    if _text(_at(outcome, "status")) != "completed":
        return None
    output = _at(outcome, "output")
    part: JsonValue = {"text": output} if isinstance(output, str) else {"data": output}
    return ({"artifactId": artifact_id(view.task_id), "name": "output", "parts": [part]},)


def open_question(view: Slice) -> OpenAsk | None:
    """The question a caller may answer: only while the task really is INPUT_REQUIRED."""
    return view.question if _state_of(view)[0] == "TASK_STATE_INPUT_REQUIRED" else None


def _at(value: JsonValue, key: str) -> JsonValue:
    """One field of the RunOutcome the shared reducer produced."""
    return value.get(key) if isinstance(value, dict) else None


def _text(value: JsonValue) -> str:
    return value if isinstance(value, str) else ""


def _number(value: JsonValue) -> str:
    """A numeric limit as the status message spells it, the same in both languages."""
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else ""


def _rfc3339(ms: int) -> str:
    """RFC 3339 UTC with milliseconds, spelled as JavaScript's toISOString does, so a TypeScript
    host and a Python host answer the same bytes for the same committed event."""
    moment = datetime.fromtimestamp(ms / 1000, UTC)
    return f"{moment.strftime('%Y-%m-%dT%H:%M:%S')}.{moment.microsecond // 1000:03d}Z"
