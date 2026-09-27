"""The outbound send, as the uncertainty contract's Part 1 exactly.

One append of `remote_call` and `effect_begin` before any byte leaves, a commit on the peer's
receipt, and nothing else. A lost answer is never a failure result and never a silent re-send: it
is `unknown`, and the loop parks it (invariant 3)."""

import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from threads._generated.a2a_v1 import Task
from threads.a2a.outbound.card import Unresolved, resolve_card
from threads.a2a.outbound.derive import claim_of, context_id_of, message_id_of
from threads.a2a.outbound.exchange import (
    Followed,
    Sent,
    SentFault,
    SentMessage,
    SentTask,
    follow,
    preview,
    send_stored,
    sent,
    status_of,
    task_text,
)
from threads.a2a.outbound.log import (
    called_in,
    inbound_claim,
    observed,
    owns_task,
    tenant_of,
)
from threads.a2a.outbound.request import Building, request_body
from threads.a2a.outbound.states import state_drafts
from threads.a2a.protocol import (
    IDEMPOTENT_SEND,
    A2aFault,
    Answer,
    Answered,
    Faulted,
    NotSent,
    PinnedCard,
    Sending,
    Streamed,
    Uncertain,
    text_of,
)
from threads.a2a.remote import Remote
from threads.loop.tools import (
    Dispatched,
    Invocation,
    Output,
    Prepared,
    Refused,
)
from threads.loop.tools import NotSent as NotSentRun
from threads.loop.tools import Uncertain as UncertainRun
from threads.store import Draft

if TYPE_CHECKING:
    from pydantic import JsonValue


@dataclass(frozen=True, slots=True)
class SendArgs:
    message: str
    task_id: str | None


async def begin_send(
    remote: Remote, args: SendArgs, call: Invocation, sending: Sending
) -> Prepared:
    """What this attempt must make durable with its `effect_begin`: the card on the thread's first
    call of the remote, then the `remote_call` holding the exact bytes. A refusal sends nothing."""
    if args.task_id is not None and not owns_task(call.events(), remote.name, args.task_id):
        return Refused(f"unknown_task: {args.task_id} was not created by this conversation")
    # An earlier attempt of this call already stored its bytes; rule 58 allows one remote_call.
    if called_in(call.events(), call.call_id) is not None:
        return ()
    card = await resolve_card(remote, call, sending)
    if isinstance(card, Unresolved):
        return Refused(card.why)
    thread_id = call.thread_id or ""
    claim = inbound_claim(call.events())
    body = request_body(
        Building(
            card.card.wire,
            message_id_of(call.branch_id or "", call.call_id),
            context_id_of(thread_id, remote.name),
            args.task_id,
            args.message,
            None
            if remote.provenance == "none"
            else claim_of(tenant_of(call.events()), claim.request or thread_id, claim.hops + 1),
        )
    )
    request_ref = await call.put(body.body, "application/json")
    data: dict[str, JsonValue] = {
        "call_id": call.call_id,
        "remote": remote.name,
        "operation": "send_message",
        "message_id": body.message_id,
        "context_id": body.context_id,
        "request_ref": request_ref.model_dump(mode="json"),
    }
    if args.task_id is not None:
        data["task_id"] = args.task_id
    return (
        *(() if card.draft is None else (card.draft,)),
        Draft("remote_call", data, {"kind": "host"}, True),
    )


async def run_send(remote: Remote, call: Invocation, sending: Sending) -> Dispatched:
    """Sends the bytes the `remote_call` named, and reports exactly what the attempt established."""
    called = called_in(call.events(), call.call_id)
    if called is None:
        # Unreachable: begin appends it in the same transaction as the effect_begin.
        return UncertainRun("transport_error")
    card = await resolve_card(remote, call, sending)
    if isinstance(card, Unresolved):
        return _failure(card.why)
    stored = await call.read(called.request_ref)
    if stored is None:
        return _failure("the stored request could not be read")
    answer = await send_stored(card.card.wire, stored.decode(), headers(card.card, sending))
    return await _answered(answer, card.card, call, sending)


async def _answered(
    answer: Answer, card: PinnedCard, call: Invocation, sending: Sending
) -> Dispatched:
    match answer:
        case NotSent():
            return NotSentRun()
        case Uncertain(reason=reason):
            return UncertainRun(reason)
        case Streamed():
            # SendMessage is not a streaming operation: an SSE answer to it is unreadable, and the
            # request was received, so the outcome is in doubt.
            return UncertainRun("transport_error")
        case Faulted(fault=f):
            return _faulted(f)
        case Answered(value=value):
            return await _read(sent(value), card, call, sending)


async def _read(read: Sent, card: PinnedCard, call: Invocation, sending: Sending) -> Dispatched:
    """The peer's own answer: an error it named, a final message, or a task we now hold."""
    match read:
        case SentFault(fault=f):
            return _faulted(f)
        case SentMessage(message=message):
            # A bare Message is final: no task to follow and no receipt to hold.
            return Output(text_of(message.parts))
        case SentTask(task=task):
            return await _committed(task, card, call, sending)


async def _committed(
    first: Task, card: PinnedCard, call: Invocation, sending: Sending
) -> Dispatched:
    followed: Followed = await follow(
        card.wire,
        first,
        headers(card, sending),
        int(time.time() * 1000) + sending.timeout_ms,
    )
    status = status_of(followed.task.status.state)
    drafts = await state_drafts(
        call, call.call_id, followed.seen, observed(call.events(), call.call_id)
    )
    return Output(
        preview(status, followed.task.id, task_text(followed.task)),
        is_error=status in ("failed", "rejected"),
        events=drafts,
        receipt=followed.task.id,
    )


def _faulted(f: A2aFault) -> Output:
    """A peer's A2A error is an answer, not a doubt: the call fails with it and the effect
    commits."""
    return Output(f"{f.name}: {f.message}", is_error=True)


def _failure(why: str) -> Output:
    """A refusal the run reaches after its begin is durable. It changed nothing at the partner, but
    the attempt is recorded, so it is reported as an error result rather than as uncertainty."""
    return Output(why, is_error=True)


def headers(card: PinnedCard, sending: Sending) -> Sending:
    """The extension header this attempt sends; the credential is already on `sending`."""
    if card.dedup_window_ms is None:
        return sending
    return replace(sending, extensions=(IDEMPOTENT_SEND,))
