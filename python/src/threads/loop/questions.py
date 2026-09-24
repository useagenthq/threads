"""ask_user in the loop (spec/schema/README.md, "Questions and remembered rules"): a valid
question parks the branch for 24 hours; one the question rules refuse is an error result. An
unanswered question past its expiry is closed with "no answer" by the next run on the branch."""

from typing import TYPE_CHECKING, Final

from threads._generated.tools_v1 import AskUserInput
from threads.log import ParkedEvent
from threads.log.ask_user import ask_problem
from threads.loop.drafts import draft
from threads.loop.history import CallState
from threads.loop.results import As, result_draft
from threads.loop.runtime import Halt, Parked, Runtime, lost
from threads.result import Err
from threads.store import Draft
from threads.store.lines import uuid7

if TYPE_CHECKING:
    from pydantic import JsonValue

TTL_MS: Final = 86_400_000
NO_ANSWER: Final = "no answer"


async def ask(rt: Runtime, state: CallState) -> Halt | None:
    """Parks on {input, call_id}, or closes the call when its input breaks the question rules."""
    call_id = state.call.data.call_id
    why = ask_problem(AskUserInput.model_validate(dict(state.call.data.input)))
    if why is not None:
        result = await result_draft(rt, call_id, f"invalid input: {why}", As("not_executed", True))
        done = await rt.append(result)
        return lost(done.error) if isinstance(done, Err) else None
    data: dict[str, JsonValue] = {
        "address": {"kind": "input", "id": call_id},
        "reason": "awaiting_input",
        "expires_at": rt.clock() + TTL_MS,
    }
    done = await rt.append(draft("parked", data))
    if isinstance(done, Err):
        return lost(done.error)
    return Parked("awaiting_input", tuple(rt.fold.parked))


async def expire(rt: Runtime) -> Halt | None:
    """Each open question whose park has expired: the error result "no answer", then resumed."""
    now = rt.clock()
    for event in list(rt.events):
        if not isinstance(event, ParkedEvent) or event.data.address not in rt.fold.parked:
            continue
        address, at = event.data.address, event.data.expires_at
        if address.kind != "input" or not isinstance(at, int) or at > now:
            continue
        cause = uuid7(now)
        data: dict[str, JsonValue] = {
            "call_id": address.id,
            "is_error": True,
            "origin": "not_executed",
            "completeness": "complete",
            "preview": NO_ANSWER,
        }
        resumed: dict[str, JsonValue] = {
            "address": {"kind": "input", "id": address.id},
            "cause_event_id": cause,
        }
        done = await rt.append(
            Draft("tool_result", data, {"kind": "host"}, True, cause), draft("resumed", resumed)
        )
        if isinstance(done, Err):
            return lost(done.error)
    return None
