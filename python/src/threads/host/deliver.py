"""A channel thread's replies, derived from its log (spec/schema/README.md, "Channel replies"),
never from how its run started.

Sources: the last response of each turn that ended `end_turn`, and each `approval_requested`
whose challenge is open. Op `i` of `adapter.render(source)` is the host-issued `channel_send`
call `send_<source seq>_<i>`, its input the op plus the conversation's address and installation
and `last_inbound_at`. After every run of the thread, and for every channel thread once a host
starts, each derived call the log lacks is issued; a pending one runs on through the effect
path; one with a result, or whose effect is parked, is left alone. So a crash between a turn's
end and its reply's `tool_call` loses no reply and never sends one twice.
"""

from typing import TYPE_CHECKING

from threads.agents.intake import After
from threads.host.send import NAME, Conversation
from threads.log import (
    ApprovalRequestedEvent,
    CallId,
    ChannelDeliveryEvent,
    Event,
    EventId,
    JsonObject,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    ParkAddress,
    TurnCompletedEvent,
    UserInputEvent,
)
from threads.loop import calls
from threads.loop.drafts import draft
from threads.loop.runtime import Halt, Runtime, lost
from threads.reduce import Fold
from threads.result import Err
from threads.store import Draft
from threads.thread import approvals

if TYPE_CHECKING:
    from pydantic import JsonValue

type Op = tuple[CallId, JsonObject, EventId]
"""A derived send: its call id, its input and the model request it answers."""


def deliver(to: Conversation) -> After:
    """The host's outbound work after a channel thread's run."""

    async def after(rt: Runtime) -> Halt | None:
        for call_id, op, request_id in undelivered(rt.fold, to):
            if call_id not in rt.fold.calls:
                issued = await rt.append(*_issue(call_id, op, request_id))
                if isinstance(issued, Err):
                    return lost(issued.error)
            stopped = await calls.run_call(rt, call_id)
            if stopped is not None:
                return stopped
        return None

    return after


def undelivered(fold: Fold, to: Conversation) -> list[Op]:
    """Each derived send still to do: its call missing, or pending with no parked effect."""
    ops: list[Op] = []
    for source, request_id, inbound_at in _sources(fold):
        for index, rendered in enumerate(to.adapter.render(source)):
            call_id = CallId(f"send_{source.seq}_{index}")
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


def _sources(fold: Fold) -> list[tuple[Event, EventId, int | None]]:
    """The source events, oldest first, each with the model request its send answers and the
    time of the last channel_delivery before it. ponytail: rescans the branch on every run;
    index delivered sources if logs grow long."""
    found: list[tuple[Event, EventId, int | None]] = []
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
                found.append((last, last.data.request_event_id, inbound_at))
            case ApprovalRequestedEvent(data=data) if data.challenge_id in waiting:
                request = fold.calls[data.call_id].data.request_event_id
                found.append((event, request, inbound_at))
            case _:
                pass
    return found


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
