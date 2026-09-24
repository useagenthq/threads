"""A requested compaction (`Thread.compact`): carried out at the next context ladder, before
anything else, and answered by exactly one outcome. Every step is decided from the log by
`stage`, so the live loop and a resumed run make the same decision (spec/schema/README.md,
"Requested compaction and output styles")."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, assert_never

from threads.log import (
    CompactionRequestedEvent,
    Event,
    EventId,
    ModelAttemptAbandonedEvent,
    ModelRequestEvent,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
)
from threads.loop import attempt, compact, defaults
from threads.loop.drafts import ActorKind
from threads.loop.history import open_cancel
from threads.loop.runtime import Failed, Halt, Runtime
from threads.reduce import Fold


@dataclass(frozen=True, slots=True)
class Stage:
    """What the log says a request needs next.

    start: no side request yet (the remaining before_compact hooks, then attempt 1); send: a
    crash abandonment within crash_resends (attempt n+1); fallback: the first prompt_too_long
    (clear, then attempt n+1); open: the latest attempt awaits its response (recovery settles
    it first); summary: the recorded summary answers it; failed: `reason` answers it."""

    kind: Literal["start", "send", "fallback", "open", "summary", "failed"]
    attempt: int = 1
    text: str = ""
    reason: str = ""
    side: EventId | None = None


def _sides(events: Sequence[Event], request: CompactionRequestedEvent) -> list[ModelRequestEvent]:
    return [
        e
        for e in events
        if isinstance(e, ModelRequestEvent) and e.data.cause_event_id == request.event_id
    ]


def stage(events: Sequence[Event], fold: Fold, request: CompactionRequestedEvent) -> Stage:
    sides = _sides(events, request)
    if not sides:
        return Stage("start")
    last = sides[-1]
    if last.event_id in fold.open_requests:
        return Stage("open")
    for e in events:
        is_response = isinstance(e, ModelResponseEvent | ModelResponseRecoveredEvent)
        if is_response and e.data.request_event_id == last.event_id:
            text = compact.response_text(e)
            if not text:
                return Stage("failed", reason="empty_summary", side=last.event_id)
            return Stage("summary", text=text, side=last.event_id)
    return _abandoned(events, fold, sides)


def _abandoned(events: Sequence[Event], fold: Fold, sides: Sequence[ModelRequestEvent]) -> Stage:
    """The latest side attempt was abandoned: re-send, fall back, or answer."""
    ids = {s.event_id for s in sides}
    reasons = [
        e.data.reason
        for e in events
        if isinstance(e, ModelAttemptAbandonedEvent) and e.data.request_event_id in ids
    ]
    reason = reasons[-1] if reasons else None
    count = reasons.count(reason) if reason else 0
    last = sides[-1]
    following = last.data.attempt + 1
    if open_cancel(events) is not None:
        # Nothing is sent after a cancel barrier: what would be sent again is answered failed.
        failure = "prompt_too_long" if reason == "prompt_too_long" else "model_error"
        return Stage("failed", reason=failure, side=last.event_id)
    if reason == "crash" and count <= defaults.retry(fold).crash_resends:
        return Stage("send", attempt=following)
    # The one fallback is counted from the log, never from the attempt number, so a crash
    # re-send before it doesn't use it up.
    if reason == "prompt_too_long":
        if count == 1:
            return Stage("fallback", attempt=following)
        return Stage("failed", reason="prompt_too_long", side=last.event_id)
    return Stage("failed", reason="model_error", side=last.event_id)


async def requested(rt: Runtime) -> Halt | None:
    """The ladder's first layer: the unanswered request, carried out until it is answered."""
    while (request := rt.fold.compaction_request) is not None:
        halt = await _advance(rt, request, stage(rt.events, rt.fold, request))
        if halt is not None:
            return halt
    return None


async def settle_requested(rt: Runtime) -> Halt | None:
    """Recovery's part: an outcome the log already decides is appended before anything runs. A
    stage that needs a model call is left to the ladder."""
    request = rt.fold.compaction_request
    if request is None:
        return None
    next_step = stage(rt.events, rt.fold, request)
    if next_step.kind not in ("summary", "failed"):
        return None
    return await _answer(rt, request, next_step, "recovery")


async def _advance(rt: Runtime, request: CompactionRequestedEvent, step: Stage) -> Halt | None:
    match step.kind:
        case "start":
            gated = await compact.before_compact(rt, request)
            if gated is not False:
                return gated
            return await _send(rt, request, 1)
        case "send":
            return await _send(rt, request, step.attempt)
        case "fallback":
            halt = await compact.clear(rt, 0, "compaction_fallback")
            return halt or await _send(rt, request, step.attempt)
        case "open":
            raise AssertionError("recovery settles an open side request before the loop runs")
        case "summary" | "failed":
            return await _answer(rt, request, step, "host")
        case _:
            assert_never(step.kind)


async def _send(rt: Runtime, request: CompactionRequestedEvent, number: int) -> Halt | None:
    """One side attempt. A recorded outcome goes back to `stage`; a refusal that left no
    request behind answers the request here."""
    sent = await attempt.request(rt, number, "compaction", request.event_id)
    if not isinstance(sent, Failed):
        return None
    answering = compact.Answering("manual", request.event_id)
    if sent.code in ("artifact_missing", "artifact_corrupt"):
        return await compact.failed(rt, "artifact_error", None, answering)
    if sent.code == "model_error":
        sides = _sides(rt.events, request)
        latest = sides[-1].event_id if sides else None
        return await compact.failed(rt, "model_error", latest, answering)
    return sent


async def _answer(
    rt: Runtime, request: CompactionRequestedEvent, outcome: Stage, actor: ActorKind
) -> Halt | None:
    answering = compact.Answering("manual", request.event_id, actor)
    if outcome.kind == "failed":
        return await compact.failed(rt, outcome.reason, outcome.side, answering)
    first = rt.fold.first_input
    last = next((e for e in rt.events if e.seq == request.seq - 1), None)
    if first is None or last is None or outcome.side is None:
        raise AssertionError("a request follows an input and a known event (rule 30)")
    return await compact.summarized(rt, (first, last), outcome.side, outcome.text, answering)
