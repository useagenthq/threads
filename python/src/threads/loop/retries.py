"""What an abandoned turn attempt leads to.

threads owns every attempt: each retry is a new `model_request`, and each wait is a recorded,
critical `retry_scheduled` that recovery honours after a crash.
"""

from threads.log import (
    ModelAttemptAbandonedEvent,
    ModelSettings,
    RetryScheduledEvent,
)
from threads.loop import compact, gates, switch
from threads.loop.defaults import retry
from threads.loop.drafts import draft
from threads.loop.history import Step, step_events
from threads.loop.runtime import Barred, Halt, Runtime, lost
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
        entry = switch.next_fallback(rt.fold)
        if entry is not None:
            return await _fall_back(rt, abandoned, current, entry)
    return await _schedule(rt, abandoned, current)


async def _resend(rt: Runtime, current: Step) -> Halt | None:
    """A crash, timeout or broken stream: re-send under the crash budget."""
    if current.crashes <= retry(rt.fold).crash_resends:
        return await request(rt, current.attempts + 1)
    return await complete(rt, "model_unavailable")


async def _fall_back(
    rt: Runtime, abandoned: ModelAttemptAbandonedEvent, current: Step, entry: ModelSettings
) -> Halt | None:
    """before_model_switch gates the change; a deny keeps the old epoch and the attempt is
    retried on it. after_model_switch observes a change."""
    drafts, allowed = await switch.gate(rt, entry)
    if not allowed:
        denied = await rt.append(*drafts)
        return (
            lost(denied.error)
            if isinstance(denied, Err)
            else await _schedule(rt, abandoned, current)
        )
    data = {"reason": "fallback", "settings": to_json(entry), "cause_event_id": abandoned.event_id}
    done = await rt.append(*drafts, draft("settings_changed", data))
    if isinstance(done, Err):
        return lost(done.error)
    if isinstance(done, Barred):
        return None  # a cancel landed first: no switch, the cancellation step is next
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
    if isinstance(done, Barred):
        return None  # a cancel landed first: no wait, the cancellation step is next
    # notification observers: a retry wait began.
    return await gates.observe(rt, "notification", done.value[-1])
