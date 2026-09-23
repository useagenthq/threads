"""Reactive compaction: once per step after the provider says the
prompt is too long. Every layer is an appended event; nothing edits history.

The summarizer call is an ordinary recorded attempt (`model_request{purpose: compaction}`), so
it replays from the log like any request and counts toward cost. Proactive thresholds (L1/L2
triggers, L4 preflight) and L3 restore are not built yet.
"""

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING

from threads.log import (
    CallId,
    ContextEditedEvent,
    Event,
    EventId,
    ModelAttemptAbandonedEvent,
    ModelRequestEvent,
    ModelResponseEvent,
    TextPart,
    ThreadStartedEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from threads.loop import attempt, defaults
from threads.loop.drafts import draft
from threads.loop.runtime import Failed, Halt, Runtime, lost
from threads.result import Err, Ok

if TYPE_CHECKING:
    from pydantic import JsonValue


async def reactive(rt: Runtime) -> Halt | None:
    """L1, then L2 with trigger reactive. Ends appending `compacted` or `compaction_failed`; the
    loop then re-sends the turn request, or ends the turn context_exhausted."""
    ctx = defaults.context(rt.fold)
    halt = await _clear(rt, ctx.clear_results.keep_recent, "threshold")
    if halt is not None:
        return halt
    bounds = await _range(rt)
    if bounds is None:
        return await _failed(rt, "still_over_threshold", None)
    return await _summarize(rt, bounds)


async def _summarize(rt: Runtime, bounds: tuple[Event, Event]) -> Halt | None:
    """The side request; if it is itself too long, clear every clearable result and retry it
    once."""
    for side_attempt in (1, 2):
        sent = await attempt.request(rt, side_attempt, "compaction")
        if sent is None or isinstance(sent, Failed):
            return sent
        outcome = rt.events[-1]
        if isinstance(outcome, ModelResponseEvent):
            return await _compacted(rt, bounds, sent, outcome)
        too_long = isinstance(outcome, ModelAttemptAbandonedEvent) and (
            outcome.data.reason == "prompt_too_long"
        )
        if not too_long or side_attempt == 2:  # noqa: PLR2004 - the fallback retries once
            reason = "prompt_too_long" if too_long else "model_error"
            return await _failed(rt, reason, sent)
        halt = await _clear(rt, 0, "compaction_fallback")
        if halt is not None:
            return halt
    return None


def _clearable(events: Sequence[Event], keep_recent: int, exclude: Sequence[str]) -> list[CallId]:
    """Results rendered in an earlier turn request, beyond the newest `keep_recent`, not excluded
    and not already cleared."""
    names = {e.data.call_id: e.data.name for e in events if isinstance(e, ToolCallEvent)}
    cleared = {
        edit.call_id
        for e in events
        if isinstance(e, ContextEditedEvent)
        for edit in e.data.edits
        if edit.action == "clear"
    }
    last_request = max(
        (
            e.seq
            for e in events
            if isinstance(e, ModelRequestEvent) and e.data.purpose != "compaction"
        ),
        default=0,
    )
    rendered = [
        e.data.call_id for e in events if isinstance(e, ToolResultEvent) and e.seq < last_request
    ]
    older = rendered[: max(0, len(rendered) - keep_recent)]
    return [c for c in older if c not in cleared and names.get(c) not in exclude]


async def _clear(rt: Runtime, keep_recent: int, reason: str) -> Halt | None:
    exclude = defaults.context(rt.fold).clear_results.exclude_tools
    calls = _clearable(rt.events, keep_recent, exclude)
    if not calls:
        return None
    edits: list[JsonValue] = [{"call_id": c, "action": "clear"} for c in calls]
    done = await rt.append(draft("context_edited", {"reason": reason, "edits": edits}))
    return lost(done.error) if isinstance(done, Err) else None


async def _range(rt: Runtime) -> tuple[Event, Event] | None:
    """From the first event after thread_started to the end of the shortest tail that starts at a
    step boundary and still holds `keep_tail` tokens (by estimate: bytes / 4)."""
    events = rt.events
    started = next(i for i, e in enumerate(events) if isinstance(e, ThreadStartedEvent))
    first = started + 1
    keep = defaults.tokens(defaults.context(rt.fold).compact.keep_tail, rt.fold)
    whole = await rt.store.render(events)
    if isinstance(whole, Err) or first >= len(events):
        return None
    for end in range(len(events) - 1, first - 1, -1):
        if events[end].seq not in rt.fold.boundaries:
            continue
        head = await rt.store.render(events[: end + 1])
        if not isinstance(head, Ok):
            return None
        if math.ceil((len(whole.value.body) - len(head.value.body)) / 4) >= keep:
            return events[first], events[end]
    return None


async def _compacted(
    rt: Runtime, bounds: tuple[Event, Event], request_id: EventId, response: ModelResponseEvent
) -> Halt | None:
    text = "".join(p.text for p in response.data.content if isinstance(p, TextPart))
    if not text:
        return await _failed(rt, "empty_summary", request_id)
    raw = text.encode("utf-8")
    sha = await rt.store.put_artifact(raw)
    first, last = bounds
    data: dict[str, JsonValue] = {
        "from_seq": first.seq,
        "to_seq": last.seq,
        "from_event_id": first.event_id,
        "to_event_id": last.event_id,
        "summary_ref": {"sha256": sha, "bytes": len(raw), "media_type": "text/plain"},
        "summary_request_event_id": request_id,
        "trigger": "reactive",
    }
    done = await rt.append(draft("compacted", data))
    return lost(done.error) if isinstance(done, Err) else None


async def _failed(rt: Runtime, reason: str, request_id: EventId | None) -> Halt | None:
    data: dict[str, str] = {"stage": "summary", "reason": reason}
    if request_id is not None:
        data["request_event_id"] = request_id
    done = await rt.append(draft("compaction_failed", data))
    return lost(done.error) if isinstance(done, Err) else None
