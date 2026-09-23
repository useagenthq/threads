"""What a channel thread sends back, derived from its log: the final
response of every turn that ended normally, and a card for every approval still open.

Each rendered op is a host-issued `channel_send` call with the deterministic id
`send_<source seq>_<op index>`. An op whose call is missing (a crash came between the turn's end
and the send) is issued; one whose call is pending runs on through the effect path; one with a
result is done. So a reply goes out once, whenever the branch next runs.
"""

from typing import TYPE_CHECKING

from threads.agents.intake import After
from threads.host.channel import ChannelAdapter
from threads.host.send import NAME
from threads.log import (
    ApprovalRequestedEvent,
    CallId,
    Event,
    EventId,
    JsonObject,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
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


def deliver(adapter: ChannelAdapter) -> After:
    """The host's outbound work for a channel thread's runs."""

    async def after(rt: Runtime) -> Halt | None:
        for call_id, op, request_id in undelivered(rt.fold, adapter):
            if call_id not in rt.fold.calls:
                issued = await rt.append(*_issue(call_id, op, request_id))
                if isinstance(issued, Err):
                    return lost(issued.error)
            stopped = await calls.run_call(rt, call_id)
            if stopped is not None:
                return stopped
        return None

    return after


def undelivered(fold: Fold, adapter: ChannelAdapter) -> list[tuple[CallId, JsonObject, EventId]]:
    """Each op not yet sent: its call missing, or still pending."""
    ops: list[tuple[CallId, JsonObject, EventId]] = []
    for source, request_id in _sources(fold):
        for index, op in enumerate(adapter.render(source)):
            call_id = CallId(f"send_{source.seq}_{index}")
            if call_id not in fold.calls or call_id in fold.pending:
                ops.append((call_id, op, request_id))
    return ops


def _sources(fold: Fold) -> list[tuple[Event, EventId]]:
    """The source events, oldest first, each with the model request its send answers.
    ponytail: rescans the branch on every run; index delivered sources if logs grow long."""
    found: list[tuple[Event, EventId]] = []
    last: ModelResponseEvent | ModelResponseRecoveredEvent | None = None
    waiting = {p.challenge_id for p in approvals.pending(fold)}
    for event in fold.events:
        match event:
            case UserInputEvent():
                last = None
            case ModelResponseEvent() | ModelResponseRecoveredEvent():
                last = event
            case TurnCompletedEvent(data=data) if data.reason == "end_turn" and last is not None:
                found.append((last, last.data.request_event_id))
            case ApprovalRequestedEvent(data=data) if data.challenge_id in waiting:
                found.append((event, fold.calls[data.call_id].data.request_event_id))
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
        "rule_id": "host_delivery",
    }
    return draft("tool_call", call), draft("permission_decision", allow)
