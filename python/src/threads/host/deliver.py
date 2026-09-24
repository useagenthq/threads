"""A channel thread's replies, derived from its log (spec/schema/README.md, "Channel replies"),
never from how its run started.

Sources: the last response of each turn that ended `end_turn`, each `approval_requested`
whose challenge is open, and each open ask_user question and `answer_rejected` for one. Op `i` of
`adapter.render(source)` is the host-issued `channel_send` call `send_<source seq>_<i>`; a
question's op `i` of `adapter.render_text(its text)` is `question_<call_id>_<i>`, a correction's
`question_retry_<answer_rejected event_id>_<i>`. Each input is the op plus the conversation's
address and installation and `last_inbound_at`. After every run of the thread, and for every
channel thread once a host starts, each derived call the log lacks is issued; a pending one runs
on through the effect path; one with a result, or whose effect is parked, is left alone. So a
crash between a turn's end and its reply's `tool_call` loses no reply and never sends one twice.
"""

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from threads.agents.intake import After
from threads.host.channel import ChannelAdapter
from threads.host.send import NAME, Conversation
from threads.log import (
    AnswerRejectedEvent,
    ApprovalRequestedEvent,
    CallId,
    ChannelDeliveryEvent,
    Event,
    EventId,
    JsonObject,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    ParkAddress,
    ParkedEvent,
    TurnCompletedEvent,
    UserInputEvent,
)
from threads.log.ask_user import correction_text, question_text
from threads.loop import calls
from threads.loop.drafts import draft
from threads.loop.history import call_state
from threads.loop.runtime import Halt, Runtime, lost
from threads.reduce import Fold
from threads.result import Err
from threads.store import Draft
from threads.thread import approvals
from threads.thread.control import question

if TYPE_CHECKING:
    from pydantic import JsonValue

NOT_DUE: Final = "not sent: no longer due"
_NEVER_SENT: Final = frozenset({None, "safe_to_retry", "not_sent", "assume_not_done"})
"""Effect states of a send that never went out: nothing to reconcile."""

type Op = tuple[CallId, JsonObject, EventId]
"""A derived send: its call id, its input and the model request it answers."""
type Source = tuple[str, Sequence[JsonObject], EventId, int | None]
"""A source's call id stem, its rendered ops, the model request it answers and the time of the
last channel_delivery before it."""


_log = logging.getLogger(__name__)


def deliver(to: Conversation) -> After:
    """The host's outbound work after a channel thread's run."""

    async def after(rt: Runtime) -> Halt | None:
        due = undelivered(rt.fold, to)
        for call_id, op, request_id in due:
            if call_id not in rt.fold.calls:
                issued = await rt.append(*_issue(call_id, op, request_id))
                if isinstance(issued, Err):
                    _log.error("send %s not issued: %s", call_id, issued.error.message)
                    return lost(issued.error)
            elif call_id not in rt.fold.host_calls:
                # The id is a model call's (its tool_use took it): never send on the agent's call.
                _log.error("%s is a model call; that message is not sent", call_id)
                continue
            stopped = await _send(rt, call_id, due=True)
            if stopped is not None:
                return stopped
        derived = {call_id for call_id, _, _ in due}
        for call_id in [c for c in rt.fold.host_calls if c not in derived]:
            if _pending(rt.fold, call_id):
                stopped = await _send(rt, call_id, due=False)
                if stopped is not None:
                    return stopped
        return None

    return after


async def _send(rt: Runtime, call_id: CallId, *, due: bool) -> Halt | None:
    """Runs one host send on. One a crash left begun is in doubt, and is reconciled through the
    effect path whether its source is still due or not (an answered question's send too). One
    that never began and is no longer due is closed, never sent (invariant 3)."""
    effect = call_state(rt.events, call_id).effect
    if not due and effect in _NEVER_SENT:
        return await calls.close(rt, call_id, "not_executed", NOT_DUE)
    if effect == "begun":
        data: dict[str, JsonValue] = {"call_id": call_id, "reason": "crash_after_begin"}
        done = await rt.append(draft("effect_unknown", data))
        if isinstance(done, Err):
            return lost(done.error)
    return await calls.run_call(rt, call_id)


def undelivered(fold: Fold, to: Conversation) -> list[Op]:
    """Each derived send still to do: its call missing, or pending with no parked effect."""
    ops: list[Op] = []
    for stem, rendered_ops, request_id, inbound_at in _sources(fold, to.adapter):
        for index, rendered in enumerate(rendered_ops):
            call_id = CallId(f"{stem}_{index}")
            if call_id in fold.calls and not _pending(fold, call_id):
                continue
            op: JsonObject = {**rendered, "address": to.address, "installation_id": to.installation}
            if inbound_at is not None:
                op["last_inbound_at"] = inbound_at
            ops.append((call_id, op, request_id))
    return ops


def _pending(fold: Fold, call_id: CallId) -> bool:
    call = fold.calls[call_id]
    parked = ParkAddress(kind="effect", id=f"{call.branch_id}:{call_id}")
    return call_id in fold.pending and parked not in fold.parked


def _sources(fold: Fold, adapter: ChannelAdapter) -> list[Source]:
    """The sources, oldest first. ponytail: rescans the branch on every run; index delivered
    sources if logs grow long."""
    found: list[Source] = []
    last: ModelResponseEvent | ModelResponseRecoveredEvent | None = None
    inbound_at: int | None = None
    waiting = {p.challenge_id for p in approvals.pending(fold) if p.expires_at > fold.now}
    for event in fold.events:
        match event:
            case ChannelDeliveryEvent():
                inbound_at = event.time
            case UserInputEvent():
                last = None
            case ModelResponseEvent() | ModelResponseRecoveredEvent():
                last = event
            case TurnCompletedEvent(data=data) if data.reason == "end_turn" and last is not None:
                stem = f"send_{last.seq}"
                found.append((stem, adapter.render(last), last.data.request_event_id, inbound_at))
            case ApprovalRequestedEvent(data=data) if data.challenge_id in waiting:
                request = fold.calls[data.call_id].data.request_event_id
                found.append((f"send_{event.seq}", adapter.render(event), request, inbound_at))
            case _:
                found.extend(_asked(fold, event, adapter, inbound_at))
    return found


def _asked(
    fold: Fold, event: Event, adapter: ChannelAdapter, inbound_at: int | None
) -> list[Source]:
    """An open question's message, or the correction after a reply that matched none."""
    match event:
        case ParkedEvent(data=data) if data.address.kind == "input":
            call_id, stem = data.address.id, f"question_{data.address.id}"
        case AnswerRejectedEvent(data=data):
            call_id, stem = data.call_id, f"question_retry_{event.event_id}"
        case _:
            return []
    if ParkAddress(kind="input", id=call_id) not in fold.parked:
        return []
    ask = question(fold, call_id)
    text = question_text(ask) if isinstance(event, ParkedEvent) else correction_text(ask)
    request = fold.calls[CallId(call_id)].data.request_event_id
    return [(stem, adapter.render_text(text), request, inbound_at)]


def _issue(call_id: CallId, op: JsonObject, request_id: EventId) -> tuple[Draft, Draft]:
    call: dict[str, JsonValue] = {
        "call_id": call_id,
        "name": NAME,
        "input": dict(op),
        "request_event_id": request_id,
    }
    allow: dict[str, JsonValue] = {
        "call_id": call_id,
        "decision": "allow",
        "source": "policy",
        "rule_id": "channel_delivery",
    }
    return draft("tool_call", call), draft("permission_decision", allow)
