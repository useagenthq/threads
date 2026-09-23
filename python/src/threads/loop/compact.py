"""Reactive compaction: once per step after the provider says the
prompt is too long. Every layer is an appended event; nothing edits history.

The summarizer call is an ordinary recorded attempt (`model_request{purpose: compaction}`), so
it replays from the log like any request and counts toward cost. The proactive layers that
call into it are loop/ladder.py.
"""

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal

from threads.hooks.runner import COMPACT, decision_draft
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
from threads.loop.gates import said, verdict
from threads.loop.restore import restore
from threads.loop.runtime import Failed, Halt, Runtime, lost
from threads.result import Err, Ok

if TYPE_CHECKING:
    from pydantic import JsonValue

type Trigger = Literal["threshold", "reactive", "manual"]


async def reactive(rt: Runtime) -> Halt | None:
    """L1, then L2 with trigger reactive. Ends appending `compacted` or `compaction_failed`; the
    loop then re-sends the turn request, or ends the turn context_exhausted."""
    ctx = defaults.context(rt.fold)
    halt = await clear(rt, ctx.clear_results.keep_recent, "threshold")
    return halt or await summarize(rt, "reactive")


async def summarize(rt: Runtime, trigger: Trigger) -> Halt | None:
    """L2: before_compact, the recorded side request, `compacted`, then L3 restore."""
    bounds = await _range(rt)
    if bounds is None:
        return await _failed(rt, "still_over_threshold", None)
    denied = await _before_compact(rt)
    if denied is not False:
        return denied
    return await _side_request(rt, bounds, trigger)


async def _before_compact(rt: Runtime) -> Halt | Literal[False] | None:
    """A deny (or a failure) is compaction_failed{hook, hook_denied}; a guide's text is its
    decision's reason, which the side request's instruction line carries (Render v1)."""
    if not rt.hooks.has("before_compact"):
        return False
    ran = await rt.hooks.run("before_compact", COMPACT, rt.writer.state())
    drafts = [
        decision_draft("before_compact", r, verdict(r), said(r, "text") or said(r, "reason"))
        for r in ran
    ]
    denied = any(verdict(r) == "deny" for r in ran)
    if denied:
        failed = {"stage": "hook", "reason": "hook_denied"}
        drafts.append(draft("compaction_failed", failed))
    done = await rt.append(*drafts)
    if isinstance(done, Err):
        return lost(done.error)
    return None if denied else False


async def _side_request(rt: Runtime, bounds: tuple[Event, Event], trigger: Trigger) -> Halt | None:
    """The side request; if it is itself too long, clear every clearable result and retry it
    once."""
    for side_attempt in (1, 2):
        sent = await attempt.request(rt, side_attempt, "compaction")
        if sent is None or isinstance(sent, Failed):
            return sent
        outcome = rt.events[-1]
        if isinstance(outcome, ModelResponseEvent):
            return await _compacted(rt, bounds, sent, outcome, trigger)
        too_long = isinstance(outcome, ModelAttemptAbandonedEvent) and (
            outcome.data.reason == "prompt_too_long"
        )
        if not too_long or side_attempt == 2:  # noqa: PLR2004 - the fallback retries once
            reason = "prompt_too_long" if too_long else "model_error"
            return await _failed(rt, reason, sent)
        halt = await clear(rt, 0, "compaction_fallback")
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


def clearable(rt: Runtime) -> bool:
    """L1 has something to clear at the configured keep_recent."""
    ctx = defaults.context(rt.fold).clear_results
    return bool(_clearable(rt.events, ctx.keep_recent, ctx.exclude_tools))


async def clear(rt: Runtime, keep_recent: int, reason: str) -> Halt | None:
    """L1: one context_edited clearing every clearable result."""
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
        # A response's own tool calls follow it: ending there would split the pair.
        splits = end + 1 < len(events) and isinstance(events[end + 1], ToolCallEvent)
        if events[end].seq not in rt.fold.boundaries or splits:
            continue
        head = await rt.store.render(events[: end + 1])
        if not isinstance(head, Ok):
            return None
        if math.ceil((len(whole.value.body) - len(head.value.body)) / 4) >= keep:
            return events[first], events[end]
    return None


async def _compacted(
    rt: Runtime,
    bounds: tuple[Event, Event],
    request_id: EventId,
    response: ModelResponseEvent,
    trigger: Trigger,
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
        "trigger": trigger,
    }
    done = await rt.append(draft("compacted", data))
    if isinstance(done, Err):
        return lost(done.error)
    dropped = [e for e in rt.events if first.seq <= e.seq <= last.seq]
    return await restore(rt, dropped)


async def _failed(rt: Runtime, reason: str, request_id: EventId | None) -> Halt | None:
    data: dict[str, str] = {"stage": "summary", "reason": reason}
    if request_id is not None:
        data["request_event_id"] = request_id
    done = await rt.append(draft("compaction_failed", data))
    return lost(done.error) if isinstance(done, Err) else None
