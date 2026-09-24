"""What an assistant-last AI SDK request records (spec/schema/ui/README.md, "Bodies"): each
approval-responded tool part's decision, and an ask_user part's answer to the open question,
through the same controls as the REST routes. A decision the log already holds with the same
value is a no-op; another value is approval_duplicate. One decided by someone else after this
request read the log is judged the same way, against the log read again."""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from pydantic import ValidationError
from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.host_api_v1 import AiSdkPart, Answer
from threads.host.ui.common import SETTLED_MEANWHILE, UiLog, read_log
from threads.host.ui.settled import decision_of
from threads.log import CallId, ParseError, Principal
from threads.result import Err, Ok

if TYPE_CHECKING:
    from threads.host.app import Host

type Recorded = Ok[str | None] | Err[ParseError]


async def record_parts(
    host: "Host", principal: Principal, log: UiLog, parts: Sequence[AiSdkPart]
) -> Ok[list[str]] | Err[ParseError]:
    """The event ids of what it newly recorded, in order."""
    recorded: list[str] = []
    for part in parts:
        done = await _record(host, principal, log, part)
        if isinstance(done, Err):
            return done
        if done.value is not None:
            recorded.append(done.value)
    return Ok(recorded)


async def _record(host: "Host", principal: Principal, log: UiLog, part: AiSdkPart) -> Recorded:
    tool = part.type.startswith("tool-") or part.type == "dynamic-tool"
    if tool and part.state == "approval-responded":
        return await _approval(host, principal, log, part)
    if part.type == "tool-ask_user" and part.state == "output-available":
        return await _answer(host, principal, log, part)
    return Ok(None)


async def _approval(host: "Host", principal: Principal, log: UiLog, part: AiSdkPart) -> Recorded:
    approval = part.approval
    if approval is MISSING or approval.approved is MISSING:
        why = "an approval-responded part needs approval.approved"
        return Err(ParseError("invalid_request", why))
    granted = approval.approved
    logged = _already(log, approval.id, granted=granted)
    if logged is not None:
        return logged
    if granted:
        done = await log.thread.approve(approval.id, principal)
    else:
        reason = None if approval.reason is MISSING else approval.reason
        done = await log.thread.deny(approval.id, principal, reason=reason)
    if isinstance(done, Err):
        if done.error.code not in SETTLED_MEANWHILE:
            return done
        now_log = await read_log(log.thread)
        again = None if now_log is None else _already(now_log, approval.id, granted=granted)
        return done if again is None else again
    await host.resume(log.thread)
    return Ok(done.value.event_id)


def _already(log: UiLog, challenge: str, *, granted: bool) -> Recorded | None:
    """The answer to a challenge the log already decided: a no-op, or approval_duplicate."""
    logged = decision_of(log.events, challenge)
    if logged is None:
        return None
    if logged == ("granted" if granted else "denied"):
        return Ok(None)
    return Err(ParseError("approval_duplicate", f"challenge {challenge} is already {logged}"))


async def _answer(host: "Host", principal: Principal, log: UiLog, part: AiSdkPart) -> Recorded:
    call = "" if part.toolCallId is MISSING else part.toolCallId
    if not any(a.kind == "input" and a.id == call for a in log.parked):
        return Ok(None)
    try:
        answer = Answer.model_validate({"answer": None if part.output is MISSING else part.output})
    except ValidationError:
        why = "an ask_user output must be text or a list of the chosen options"
        return Err(ParseError("invalid_request", why))
    done = await log.thread.answer(CallId(call), answer.answer, principal)
    if isinstance(done, Err):
        # Answered or closed meanwhile: ignored, like any answer to a question no longer open.
        return Ok(None) if done.error.code == "no_open_question" else done
    await host.resume(log.thread)
    return Ok(done.value.event_id)
