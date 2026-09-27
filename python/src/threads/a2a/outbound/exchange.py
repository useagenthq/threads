"""One exchange with a partner: the send that carries the stored bytes, the parse of what came
back, and the follow that reads a task to a state it cannot leave on its own.

Nothing here touches the log — the caller decides what to record, because only it knows what is
durable."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final, Literal

from pydantic import ValidationError
from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.a2a_v1 import Message, Part, SendMessageResponse, Task
from threads.a2a.protocol import (
    A2aFault,
    Answer,
    Answered,
    MessagePayload,
    Sending,
    TaskPayload,
    Wire,
    call,
    fault,
    is_file_part,
    is_settled,
    payload_of,
    text_of,
)
from threads.log.jcs import canonicalize
from threads.result import Ok

type Status = Literal["completed", "needs_input", "working", "failed", "rejected", "canceled"]
"""The statuses a model is shown. `working` means the deadline passed with a receipt in hand."""

_STATUS: Final[Mapping[str, Status]] = {
    "TASK_STATE_SUBMITTED": "working",
    "TASK_STATE_WORKING": "working",
    "TASK_STATE_COMPLETED": "completed",
    "TASK_STATE_FAILED": "failed",
    "TASK_STATE_CANCELED": "canceled",
    "TASK_STATE_REJECTED": "rejected",
    "TASK_STATE_INPUT_REQUIRED": "needs_input",
    # The partner wants a credential from us, which no model may supply: a failure to report.
    "TASK_STATE_AUTH_REQUIRED": "failed",
}

FIRST_WAIT_MS: Final = 1000
MAX_WAIT_MS: Final = 30_000
"""The follow's backoff: 1s doubling to 30s. Reads are not effects, so they retry freely."""

_CUT: Final = "a file part is not supported; text and JSON parts only"


async def send_stored(wire: Wire, body: str, sending: Sending) -> Answer:
    """A `SendMessage` of bytes that already exist. `params` is empty on purpose: `SendMessage`
    carries its request in the body in both bindings, so the URL never depends on it, and the body
    the first attempt stored is sent unchanged (30-a2a decision H30-1)."""
    return await call(wire, "SendMessage", {}, replace(sending, body=body))


async def get_task(wire: Wire, task_id: str, sending: Sending) -> Answer:
    return await call(wire, "GetTask", {"id": task_id}, sending)


@dataclass(frozen=True, slots=True)
class SentTask:
    task: Task


@dataclass(frozen=True, slots=True)
class SentMessage:
    message: Message


@dataclass(frozen=True, slots=True)
class SentFault:
    fault: A2aFault


type Sent = SentTask | SentMessage | SentFault
"""What a `SendMessage` answered: a task to follow, a final bare message, or the peer's error."""


def sent(value: object) -> Sent:
    """The response body as one of the three answers. A shape we cannot read is a fault of its own:
    the peer replied, so nothing is in doubt about whether it received us."""
    try:
        parsed = SendMessageResponse.model_validate(value)
    except ValidationError:
        return SentFault(
            fault("InvalidAgentResponseError", "the peer's answer is not a SendMessageResponse")
        )
    match payload_of(parsed):
        case TaskPayload(task=task):
            return (
                SentFault(fault("ContentTypeNotSupportedError", _CUT))
                if _cut(_task_parts(task))
                else SentTask(task)
            )
        case MessagePayload(message=message):
            return (
                SentFault(fault("ContentTypeNotSupportedError", _CUT))
                if _cut(message.parts)
                else SentMessage(message)
            )
        case _:
            return SentFault(
                fault(
                    "InvalidAgentResponseError",
                    "the peer's answer is neither one task nor one message",
                )
            )


def _cut(parts: Sequence[Part]) -> bool:
    """The cut: text and JSON parts only, in either direction."""
    return any(is_file_part(p) for p in parts)


def _task_parts(task: Task) -> tuple[Part, ...]:
    status = () if task.status.message is MISSING else tuple(task.status.message.parts)
    artifacts = (
        () if task.artifacts is MISSING else tuple(p for a in task.artifacts for p in a.parts)
    )
    return (*status, *artifacts)


@dataclass(frozen=True, slots=True)
class Followed:
    """A task, and every state this exchange observed on the way to it."""

    task: Task
    seen: tuple[Task, ...]


async def _sleep(ms: int) -> None:
    await asyncio.sleep(ms / 1000)


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class Waiting:
    """The follow's clock and its sleep, injected together so a test never waits on real time."""

    now: Callable[[], int] = _now_ms
    sleep: Callable[[int], Awaitable[None]] = _sleep


REAL_TIME: Final = Waiting()


async def follow(
    wire: Wire,
    first: Task,
    sending: Sending,
    deadline: int,
    waiting: Waiting = REAL_TIME,
) -> Followed:
    """Reads the task until it is settled or the deadline passes. A read is not an effect: it never
    appends an `effect_begin` and it is free to retry. Once we hold a task id a slow peer is just a
    task still working, so the deadline stops **waiting**, never re-sends."""
    seen: list[Task] = [first]
    task = first
    backoff = FIRST_WAIT_MS
    while not is_settled(task.status.state):
        left = deadline - waiting.now()
        if left <= 0:
            break
        await waiting.sleep(min(backoff, left))
        backoff = min(backoff * 2, MAX_WAIT_MS)
        if waiting.now() >= deadline:
            break
        answer = await get_task(wire, task.id, sending)
        if not isinstance(answer, Answered):
            break
        read = sent(answer.value)
        if not isinstance(read, SentTask):
            break
        task = read.task
        seen.append(task)
    return Followed(task, tuple(seen))


def preview(status: Status, task_id: str, text: str) -> str:
    """What the model sees: canonical JSON, so the same observation renders the same bytes in both
    languages. A remote's text reaches the model as a tool result, never as an instruction."""
    shown = canonicalize({"status": status, "task_id": task_id, "text": text})
    if not isinstance(shown, Ok):
        raise AssertionError("a preview of strings is canonical JSON")
    return shown.value


def status_of(state: str) -> Status:
    return _STATUS[state]


def task_text(task: Task) -> str:
    """The text a task carries: its status message, else its artifacts."""
    status = "" if task.status.message is MISSING else text_of(task.status.message.parts)
    if status:
        return status
    if task.artifacts is MISSING:
        return ""
    return "\n".join(text_of(a.parts) for a in task.artifacts)
