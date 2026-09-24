"""An AG-UI input's `resume` entries (spec/schema/ui/README.md, "Bodies"). An entry for an
interrupt still open is recorded through the same controls as the REST routes. One for an
interrupt already settled (by the same value, another value, another approver, or expiry)
records nothing and is never an error: the stock client would be locked out of its thread.
Where its value differs from the log's, the stream says so with threads.resume_conflict."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import JsonValue, ValidationError
from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.host_api_v1 import AgUiResumeEntry, Answer, ApprovalDecision
from threads.host.ui.common import UiLog
from threads.host.ui.frame import Chunk
from threads.host.ui.settled import Approval, Other, Question, Unknown, interrupt_of
from threads.log import CallId, Event, ParkAddress, ParseError, Principal
from threads.result import Err, Ok

if TYPE_CHECKING:
    from threads.host.app import Host


@dataclass(frozen=True, slots=True)
class _Entry:
    entry: AgUiResumeEntry
    at: Approval | Question


async def apply_resume(  # noqa: PLR0913 - the request and how it came
    host: "Host",
    principal: Principal,
    log: UiLog,
    entries: Sequence[AgUiResumeEntry],
    *,
    now: int,
    new_message: bool,
) -> Ok[list[Chunk]] | Err[ParseError]:
    """The conflicts to report, once every open entry is recorded."""
    classified = _classify(log.events, log.parked, entries, now)
    if isinstance(classified, Err):
        return classified
    if new_message and _blocked(log, classified.value, now):
        return Err(ParseError("invalid_request", "answer the interrupts first"))
    for c in classified.value:
        if _acts(c):
            done = await _record(host, principal, log, c, wait=new_message)
            if isinstance(done, Err):
                return done
    return Ok([chunk for c in classified.value for chunk in _conflict(c)])


def resume_conflicts(
    events: Sequence[Event],
    parked: Sequence[ParkAddress],
    entries: Sequence[AgUiResumeEntry],
    now: int,
) -> Ok[list[Chunk]] | Err[ParseError]:
    """The conflicts entries of settled interrupts report, recording nothing (the conformance
    runner's `resume`)."""
    classified = _classify(events, parked, entries, now)
    if isinstance(classified, Err):
        return classified
    return Ok([chunk for c in classified.value for chunk in _conflict(c)])


def _classify(
    events: Sequence[Event],
    parked: Sequence[ParkAddress],
    entries: Sequence[AgUiResumeEntry],
    now: int,
) -> Ok[list[_Entry]] | Err[ParseError]:
    out: list[_Entry] = []
    for entry in entries:
        at = interrupt_of(events, parked, entry.interruptId, now)
        match at:
            case Unknown():
                return Err(ParseError("invalid_request", f"no interrupt {entry.interruptId}"))
            case Other():
                why = f"interrupt {entry.interruptId} is settled by an operator, not a resume"
                return Err(ParseError("forbidden", why))
            case Approval() | Question():
                out.append(_Entry(entry, at))
    return Ok(out)


def _acts(c: _Entry) -> bool:
    """An open interrupt is answered; `cancelled` for an expired challenge the branch is still
    parked on cancels the turn, since nothing else closes that park."""
    if c.at.state == "open":
        return True
    at = c.at
    return (
        isinstance(at, Approval)
        and at.state == "expired"
        and at.parked
        and c.entry.status == "cancelled"
    )


def _blocked(log: UiLog, classified: Sequence[_Entry], now: int) -> bool:
    """A new message waits while an entry is open or the thread still waits on an interrupt."""
    if any(c.at.state == "open" for c in classified):
        return True
    cancelled = {c.entry.interruptId for c in classified if _acts(c)}
    for a in log.parked:
        if a.kind != "approval":
            return True
        at = interrupt_of(log.events, log.parked, a.id, now)
        if (isinstance(at, Approval) and at.state == "open") or a.id not in cancelled:
            return True
    return False


async def _record(
    host: "Host", principal: Principal, log: UiLog, c: _Entry, *, wait: bool
) -> Ok[None] | Err[ParseError]:
    entry, at = c.entry, c.at
    if at.state != "open" or (isinstance(at, Question) and entry.status == "cancelled"):
        cancelled = await log.thread.cancel(principal)
        if isinstance(cancelled, Err):
            return cancelled
        await host.resume(log.thread, wait=wait)
        return Ok(None)
    if isinstance(at, Approval):
        return await _decide(host, principal, log, entry)
    answer = _answer(entry)
    if answer is None:
        why = f"resume {entry.interruptId}: a resolved question's payload is an Answer"
        return Err(ParseError("invalid_request", why))
    done = await log.thread.answer(CallId(entry.interruptId), answer.answer, principal)
    if isinstance(done, Err):
        return done
    await host.resume(log.thread)
    return Ok(None)


async def _decide(
    host: "Host", principal: Principal, log: UiLog, entry: AgUiResumeEntry
) -> Ok[None] | Err[ParseError]:
    decision = _decision(entry)
    if decision is None:
        why = f"resume {entry.interruptId}: a resolved approval's payload is an ApprovalDecision"
        return Err(ParseError("invalid_request", why))
    challenge = entry.interruptId
    if decision.decision == "grant":
        rule = None if decision.remember_rule is MISSING else decision.remember_rule
        done = await log.thread.approve(challenge, principal, remember_rule=rule)
    else:
        reason = None if decision.reason is MISSING else decision.reason
        done = await log.thread.deny(challenge, principal, reason=reason)
    if isinstance(done, Err):
        return done
    await host.resume(log.thread)
    return Ok(None)


def _decision(entry: AgUiResumeEntry) -> ApprovalDecision | None:
    """A resolved entry's decision (its payload), or cancelled as a denial."""
    if entry.status == "cancelled":
        return ApprovalDecision(decision="deny")
    if entry.payload is MISSING:
        return None
    try:
        return ApprovalDecision.model_validate(entry.payload)
    except ValidationError:
        return None


def _granted(entry: AgUiResumeEntry) -> bool | None:
    decision = _decision(entry)
    return None if decision is None else decision.decision == "grant"


def _answer(entry: AgUiResumeEntry) -> Answer | None:
    if entry.payload is MISSING:
        return None
    try:
        return Answer.model_validate(entry.payload)
    except ValidationError:
        return None


def _conflict(c: _Entry) -> list[Chunk]:
    """threads.resume_conflict when a settled interrupt's entry differs from the log."""
    state = c.at.state
    if state == "open" or not _differs(c):
        return []
    value: JsonValue = {"interruptId": c.entry.interruptId, "recorded": state}
    return [{"type": "CUSTOM", "name": "threads.resume_conflict", "value": value}]


def _differs(c: _Entry) -> bool:
    entry, at = c.entry, c.at
    match at.state:
        case "granted":
            return _granted(entry) is not True
        case "denied":
            return _granted(entry) is not False
        case "expired" | "cancelled":
            return entry.status != "cancelled"
        case "answered":
            answer = _answer(entry)
            if answer is None or not isinstance(at, Question):
                return True
            given = answer.answer
            text = given if isinstance(given, str) else "\n".join(given)
            return text != at.answer
        case "open":
            return False
