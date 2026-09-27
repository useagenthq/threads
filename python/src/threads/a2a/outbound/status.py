"""`<name>_status`: one GetTask, or a follow to the deadline.

It is read_only and sends no message, so it never begins an effect and never asks for approval — and
it still writes `remote_task_state` for each state change it observes, under the call that created
the task, because an observation belongs to the receipt it was read against (semantic rule 57)."""

import time

from threads._generated.a2a_v1 import Task
from threads.a2a.outbound.card import Unresolved, resolve_card
from threads.a2a.outbound.exchange import (
    Sent,
    SentFault,
    SentTask,
    follow,
    get_task,
    preview,
    sent,
    status_of,
    task_text,
)
from threads.a2a.outbound.log import observed, owns_task, task_owner
from threads.a2a.outbound.send import headers
from threads.a2a.outbound.states import state_drafts
from threads.a2a.protocol import Answer, Answered, Faulted, Sending
from threads.a2a.remote import Remote
from threads.loop.tools import Dispatched, Invocation, Output


def sent_of(answer: Answer) -> Sent | str:
    """What the peer answered, or why this read has no answer to show."""
    if isinstance(answer, Faulted):
        return f"{answer.fault.name}: {answer.fault.message}"
    if not isinstance(answer, Answered):
        return "the read did not finish"
    return sent(answer.value)


def _task(read: Sent | str) -> Task | str:
    """The task the read found, or the message a model is shown instead."""
    if isinstance(read, str):
        return read
    if isinstance(read, SentFault):
        return f"{read.fault.name}: {read.fault.message}"
    if not isinstance(read, SentTask):
        return "the peer answered a message, not a task"
    return read.task


async def run_status(
    remote: Remote, task_id: str, call: Invocation, sending: Sending
) -> Dispatched:
    # All tenants' calls share one host credential, so a task this thread did not create is not
    # ours to read: nothing is sent and nothing is read.
    if not owns_task(call.events(), remote.name, task_id):
        return Output(
            f"unknown_task: {task_id} was not created by this conversation", is_error=True
        )
    card = await resolve_card(remote, call, sending)
    if isinstance(card, Unresolved):
        return Output(card.why, is_error=True)
    sending_with = headers(card.card, sending)
    read = _task(sent_of(await get_task(card.card.wire, task_id, sending_with)))
    if isinstance(read, str):
        return Output(read, is_error=True)
    followed = await follow(
        card.card.wire,
        read,
        sending_with,
        int(time.time() * 1000) + sending.timeout_ms,
    )
    owner = task_owner(call.events(), remote.name, task_id)
    status = status_of(followed.task.status.state)
    drafts = (
        ()
        if owner is None
        else await state_drafts(call, owner, followed.seen, observed(call.events(), owner))
    )
    return Output(
        preview(status, followed.task.id, task_text(followed.task)),
        is_error=status in ("failed", "rejected"),
        events=drafts,
    )
