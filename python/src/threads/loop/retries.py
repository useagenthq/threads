"""What an abandoned turn attempt leads to.

threads owns every attempt: each retry is a new `model_request`, and each wait is a recorded,
critical `retry_scheduled` that recovery honours after a crash.
"""

from threads.hooks.runner import SWITCH, decision_draft
from threads.log import (
    ModelAttemptAbandonedEvent,
    ModelSettings,
    RetryScheduledEvent,
    SettingsChangedEvent,
)
from threads.loop import compact, gates
from threads.loop.defaults import fallbacks, retry
from threads.loop.drafts import draft
from threads.loop.gates import said, verdict
from threads.loop.history import Step, step_events
from threads.loop.runtime import Halt, Runtime, lost
from threads.loop.turn import complete, request
from threads.reduce.handlers import to_json
from threads.result import Err

_RETRYABLE = ("rate_limited", "overloaded", "server_error")


async def after_abandon(
    rt: Runtime, abandoned: ModelAttemptAbandonedEvent, current: Step
) -> Halt | None:
    reason = abandoned.data.reason
    policy = retry(rt.fold)
    if abandoned.data.provider_outcome in ("unknown", "not_sent"):
        return await _resend(rt, current)
    if reason == "prompt_too_long":
        return await (
            complete(rt, "context_exhausted") if current.compacted else compact.reactive(rt)
        )
    if reason not in _RETRYABLE:
        return await complete(rt, "error")
    if current.retryable > policy.max_retries:
        return await complete(rt, "model_unavailable")
    if reason == "overloaded" and current.overloaded_run >= policy.fallback_after:
        entry = _next_fallback(rt)
        if entry is not None:
            return await _fall_back(rt, abandoned, entry)
    return await _schedule(rt, abandoned, current)


async def _resend(rt: Runtime, current: Step) -> Halt | None:
    """A crash, timeout or broken stream: re-send under the crash budget."""
    if current.crashes <= retry(rt.fold).crash_resends:
        return await request(rt, current.attempts + 1)
    return await complete(rt, "model_unavailable")


def _next_fallback(rt: Runtime) -> ModelSettings | None:
    entries = fallbacks(rt.fold)
    current = next(
        (e.data.settings for e in reversed(rt.events) if isinstance(e, SettingsChangedEvent)), None
    )
    index = 0
    for i, entry in enumerate(entries):
        if current is not None and to_json(entry) == to_json(current):
            index = i + 1
    return entries[index] if index < len(entries) else None


async def _fall_back(
    rt: Runtime, abandoned: ModelAttemptAbandonedEvent, entry: ModelSettings
) -> Halt | None:
    """before_model_switch gates the change (a deny or a failure: no fallback, the turn ends
    model_unavailable); after_model_switch observes it."""
    ran = await rt.hooks.run("before_model_switch", SWITCH, entry)
    drafts = [decision_draft("before_model_switch", r, verdict(r), said(r, "reason")) for r in ran]
    if any(verdict(r) != "allow" for r in ran):
        drafts.append(draft("turn_completed", {"reason": "model_unavailable"}))
        done = await rt.append(*drafts)
        return lost(done.error) if isinstance(done, Err) else None
    data = {"reason": "fallback", "settings": to_json(entry), "cause_event_id": abandoned.event_id}
    done = await rt.append(*drafts, draft("settings_changed", data))
    if isinstance(done, Err):
        return lost(done.error)
    return await gates.observe(rt, "after_model_switch", entry)


async def _schedule(
    rt: Runtime, abandoned: ModelAttemptAbandonedEvent, current: Step
) -> Halt | None:
    policy = retry(rt.fold)
    after = abandoned.data.retry_after_ms
    if abandoned.data.reason == "rate_limited" and isinstance(after, int):
        if after > policy.max_retry_after_ms:
            return await complete(rt, "model_unavailable")
        delay, basis = after, "retry_after"
    else:
        backoff = policy.base_delay_ms * 2 ** (current.retryable - 1)
        delay, basis = min(policy.max_delay_ms, backoff), "backoff"
    events = step_events(rt.events)
    waited = sum(e.data.delay_ms for e in events if isinstance(e, RetryScheduledEvent))
    if waited + delay > policy.max_total_wait_ms:
        return await complete(rt, "model_unavailable")
    data = {
        "request_event_id": abandoned.data.request_event_id,
        "delay_ms": delay,
        "not_before": rt.clock() + delay,
        "basis": basis,
    }
    done = await rt.append(draft("retry_scheduled", data))
    if isinstance(done, Err):
        return lost(done.error)
    # notification observers: a retry wait began.
    return await gates.observe(rt, "notification", done.value[-1])
