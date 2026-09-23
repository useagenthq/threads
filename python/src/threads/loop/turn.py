"""The two ways a step ends the loop's current decision: a turn request, or the turn's end."""

from threads.loop import attempt, gates, ladder
from threads.loop.budget import reserve
from threads.loop.drafts import draft
from threads.loop.runtime import Failed, Halt, Runtime, lost
from threads.result import Err


async def request(rt: Runtime, attempt_no: int) -> Halt | None:
    """A turn request, once its input and model gates passed and its budget
    reservation fits."""
    gated = await gates.before_request(rt) or await ladder.fit(rt)
    if gated is not None:
        return None if gated == gates.AGAIN else gated
    refused = await reserve(rt)
    if refused is not None:
        return refused
    sent = await attempt.request(rt, attempt_no)
    return sent if isinstance(sent, Failed) else None


async def complete(rt: Runtime, reason: str) -> Halt | None:
    done = await rt.append(draft("turn_completed", {"reason": reason}))
    return lost(done.error) if isinstance(done, Err) else await gates.failed(rt, reason)
