"""Reading the A2A data model: which states are terminal, which oneof a payload set, and what
text a set of parts carries. The message shapes themselves are generated from
spec/schema/a2a.v1.schema.json (`threads._generated.a2a_v1`), so there is one source for both
languages.

`TASK_STATE_UNSPECIFIED` is the proto's zero value and means "unknown or indeterminate", which is
never an answer we can act on, so the generated `TaskState` leaves it out and a peer that sends it
fails the parse."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.a2a_v1 import (
    Message,
    Part,
    SendMessageResponse,
    StreamResponse,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
)

TASK_STATES: Final[tuple[TaskState, ...]] = (
    "TASK_STATE_SUBMITTED",
    "TASK_STATE_WORKING",
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_REJECTED",
    "TASK_STATE_AUTH_REQUIRED",
)

_TERMINAL: Final[frozenset[str]] = frozenset(
    {
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELED",
        "TASK_STATE_REJECTED",
    }
)
_INTERRUPTED: Final[frozenset[str]] = frozenset(
    {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"}
)


def is_terminal(state: TaskState) -> bool:
    """Terminal per the pinned spec: completed, failed, canceled, rejected."""
    return state in _TERMINAL


def is_interrupted(state: TaskState) -> bool:
    """Interrupted per the pinned spec: input-required, auth-required."""
    return state in _INTERRUPTED


def is_settled(state: TaskState) -> bool:
    """A stream closes, and a follow stops, once the task can go no further on its own."""
    return state in _TERMINAL or state in _INTERRUPTED


@dataclass(frozen=True, slots=True)
class TaskPayload:
    task: Task


@dataclass(frozen=True, slots=True)
class MessagePayload:
    message: Message


type Payload = TaskPayload | MessagePayload


def payload_of(response: SendMessageResponse) -> Payload | None:
    """The one payload a response set, or None when it set none or several. A oneof is one key in
    JSON, and "exactly one" is a rule JSON Schema would need `oneOf` for, so it is decided here."""
    task, message = response.task, response.message
    if task is not MISSING and message is MISSING:
        return TaskPayload(task)
    if message is not MISSING and task is MISSING:
        return MessagePayload(message)
    return None


@dataclass(frozen=True, slots=True)
class StatusPayload:
    status: TaskStatusUpdateEvent


@dataclass(frozen=True, slots=True)
class ArtifactPayload:
    artifact: TaskArtifactUpdateEvent


type StreamPayloadKind = Literal["task", "message", "status", "artifact"]
type StreamPayload = TaskPayload | MessagePayload | StatusPayload | ArtifactPayload


def stream_payload(item: StreamResponse) -> StreamPayload | None:
    """The one payload a stream item set, or None when it set none or several."""
    task, message = item.task, item.message
    status, artifact = item.statusUpdate, item.artifactUpdate
    found: list[StreamPayload] = []
    if task is not MISSING:
        found.append(TaskPayload(task))
    if message is not MISSING:
        found.append(MessagePayload(message))
    if status is not MISSING:
        found.append(StatusPayload(status))
    if artifact is not MISSING:
        found.append(ArtifactPayload(artifact))
    return found[0] if len(found) == 1 else None


def text_of(parts: Sequence[Part]) -> str:
    """The text of every text part, joined: what a model is shown of a remote's answer."""
    return "".join(p.text for p in parts if p.text is not MISSING)


def is_file_part(part: Part) -> bool:
    """A file part, which the cut refuses wherever it arrives: text and JSON parts only."""
    return part.raw is not MISSING or part.url is not MISSING
