"""Reactive compaction (with L1 and L2): once per step after the provider says the
prompt is too long. Every layer is an appended event; nothing edits history.

The summarizer call is an ordinary recorded attempt (`model_request{purpose: compaction}`), so
it replays from the log like any request and counts toward cost. The proactive layers that
call into it are loop/ladder.py; a requested compaction is loop/manual.py.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from threads.hooks.runner import COMPACT, decision_draft, run_one
from threads.hooks.types import wire_name
from threads.log import (
    CallId,
    CompactionRequestedEvent,
    ContextEditedEvent,
    Event,
    EventId,
    HookDecisionEvent,
    ModelAttemptAbandonedEvent,
    ModelRequestEvent,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    TextPart,
    ThreadStartedEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from threads.loop import attempt, defaults
from threads.loop.drafts import ActorKind, draft
from threads.loop.gates import said, verdict
from threads.loop.restore import restore_drafts, restore_hooks
from threads.loop.runtime import Failed, Halt, Runtime, lost
from threads.result import Err, Ok

if TYPE_CHECKING:
    from pydantic import JsonValue

type Trigger = Literal["threshold", "reactive", "manual"]


@dataclass(frozen=True, slots=True)
class Answering:
    """Who a compaction's outcome is for: its trigger, the compaction_requested it answers, and
    recovery when recovery settles it."""

    trigger: Trigger = "threshold"
    cause: EventId | None = None
    actor: ActorKind = "host"

    def tag(self, data: "dict[str, JsonValue]") -> "dict[str, JsonValue]":
        return data if self.cause is None else {**data, "cause_event_id": self.cause}


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
        return await failed(rt, "still_over_threshold", None, Answering(trigger))
    denied = await before_compact(rt)
    if denied is not False:
        return denied
    return await _side_request(rt, bounds, trigger)


async def before_compact(
    rt: Runtime, request: CompactionRequestedEvent | None = None
) -> Halt | Literal[False] | None:
    """A deny (or a failure) is compaction_failed{hook, hook_denied}; a guide's text is its
    decision's reason, which the side request's instruction line carries (Render v1). Each
    decision is appended before the next extension runs, and every extension sees the state
    from before the first call. For a request, an extension that already decided after it is not
    asked again. False: go on to the side request."""
    decided = _decided(rt.events, request)
    state = rt.writer.state()
    for bound in rt.hooks.defining("before_compact"):
        if bound.extension in decided:
            continue
        ran = await run_one(bound, "before_compact", COMPACT, state)
        decision = verdict(ran)
        drafts = [
            decision_draft(
                "before_compact", ran, decision, said(ran, "text") or said(ran, "reason")
            )
        ]
        denied = decision == "deny"
        if denied:
            cause = Answering(cause=None if request is None else request.event_id)
            drafts.append(
                draft("compaction_failed", cause.tag({"stage": "hook", "reason": "hook_denied"}))
            )
        done = await rt.append(*drafts)
        if isinstance(done, Err):
            return lost(done.error)
        if denied:
            return None
    return False


def _decided(events: Sequence[Event], request: CompactionRequestedEvent | None) -> set[str]:
    """Extensions with a before_compact decision recorded after the request."""
    if request is None:
        return set()
    hook = wire_name("before_compact")
    return {
        e.data.extension
        for e in events
        if isinstance(e, HookDecisionEvent) and e.seq > request.seq and e.data.hook == hook
    }


async def _side_request(rt: Runtime, bounds: tuple[Event, Event], trigger: Trigger) -> Halt | None:
    """The side request; if it is itself too long, clear every clearable result and retry it
    once."""
    for side_attempt in (1, 2):
        sent = await attempt.request(rt, side_attempt, "compaction")
        if sent is None or isinstance(sent, Failed):
            return sent
        outcome = rt.events[-1]
        if isinstance(outcome, ModelResponseEvent):
            text = response_text(outcome)
            if not text:
                return await failed(rt, "empty_summary", sent, Answering(trigger))
            return await summarized(rt, bounds, sent, text, Answering(trigger))
        too_long = isinstance(outcome, ModelAttemptAbandonedEvent) and (
            outcome.data.reason == "prompt_too_long"
        )
        if not too_long or side_attempt == 2:  # noqa: PLR2004 - the fallback retries once
            reason = "prompt_too_long" if too_long else "model_error"
            return await failed(rt, reason, sent, Answering(trigger))
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


def response_text(response: ModelResponseEvent | ModelResponseRecoveredEvent) -> str:
    return "".join(p.text for p in response.data.content if isinstance(p, TextPart))


async def summarized(
    rt: Runtime, bounds: tuple[Event, Event], request_id: EventId, text: str, answering: Answering
) -> Halt | None:
    """`compacted` over `bounds` with its restore in the same batch; the context hooks follow."""
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
        "trigger": answering.trigger,
    }
    dropped = [e for e in rt.events if first.seq <= e.seq <= last.seq]
    restored = await restore_drafts(rt, dropped)
    done = await rt.append(draft("compacted", answering.tag(data), answering.actor), *restored)
    if isinstance(done, Err):
        return lost(done.error)
    return await restore_hooks(rt)


async def failed(
    rt: Runtime, reason: str, request_id: EventId | None, answering: Answering
) -> Halt | None:
    data: dict[str, JsonValue] = {"stage": "summary", "reason": reason}
    if request_id is not None:
        data["request_event_id"] = request_id
    done = await rt.append(draft("compaction_failed", answering.tag(data), answering.actor))
    return lost(done.error) if isinstance(done, Err) else None
