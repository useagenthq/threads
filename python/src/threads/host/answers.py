"""A channel reply to an open ask_user question (spec/schema/README.md, "Questions and remembered
rules"): while a question is open, the asker's next message answers the oldest one. A reply that
is an option is recorded as the answer; one that isn't is consumed as `answer_rejected`, and the
question stays open. Either is one append with the inbox item's consumption."""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal

from threads.log import ParkedEvent, ParseError, Principal
from threads.reduce import Fold
from threads.result import Err, Ok
from threads.store import Draft
from threads.store.companion import Companion
from threads.thread.control import answered, answering, append, requester
from threads.thread.handle import Thread

if TYPE_CHECKING:
    from pydantic import JsonValue

type Taken = Literal["answered", "rejected", "held", "busy", "no_question"]
"""answered or rejected: consumed. held: another sender's message while a question is open; it
waits as ordinary input without blocking the asker's reply. busy: another holder has the branch.
no_question: nothing is asked, the message is ordinary input."""


def oldest(fold: Fold) -> str | None:
    """The call id of the oldest open question, if any."""
    for event in fold.events:
        address = event.data.address if isinstance(event, ParkedEvent) else None
        if address is not None and address.kind == "input" and address in fold.parked:
            return address.id
    return None


async def reply(
    thread: Thread,
    delivery: Draft,
    text: str,
    principal: Principal,
    consume: Companion,
) -> Taken:
    """The reply as the answer to the oldest open question, when the asker sent it."""
    outcome: list[Taken] = ["no_question"]

    def build(fold: Fold) -> Ok[Sequence[Draft]] | Err[ParseError]:
        call_id = oldest(fold)
        if call_id is None:
            return Err(ParseError("no_open_question", "nothing is asked"))
        outcome[0] = "held"
        if requester(fold.events) != principal:
            return Err(ParseError("forbidden", "only the asker answers"))
        recorded = answering(fold, call_id, text, principal)
        if isinstance(recorded, Ok):
            outcome[0] = "answered"
            return Ok((delivery, *answered(fold, call_id, recorded.value, principal)))
        if recorded.error.code != "invalid_answer":
            return recorded
        outcome[0] = "rejected"
        data: dict[str, JsonValue] = {
            "call_id": call_id,
            "delivery_event_id": delivery.event_id,
            "reason": "not_an_option",
        }
        return Ok((delivery, Draft("answer_rejected", data)))

    done = await append(thread.store, thread.branch, build, consume)
    if isinstance(done, Err) and done.error.code == "branch_busy":
        return "busy"
    if isinstance(done, Err) and outcome[0] in ("answered", "rejected"):
        # The append itself failed (a race lost to another settler): look again later.
        return "busy"
    return outcome[0]
