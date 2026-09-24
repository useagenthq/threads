"""The context ladder before every turn request: L1 clears old results, L2
compacts on its threshold (then L3 restores), and L4 blocks a request the estimate says can't
fit, before any model_request exists. Each layer runs only while the estimate is still over its
trigger, and each is an appended event: nothing edits history, and line 0 never changes (C7)."""

from collections.abc import Sequence

from threads.log import (
    CompactedEvent,
    CompactionFailedEvent,
    ContextPreflightBlockedEvent,
    Event,
    ModelRequestEvent,
    ModelResponseEvent,
)
from threads.loop import compact, defaults, manual
from threads.loop.drafts import draft
from threads.loop.estimate import estimate
from threads.loop.gates import AGAIN, Gated, append
from threads.loop.history import open_cancel, turn_events
from threads.loop.runtime import Runtime
from threads.result import Err


async def fit(rt: Runtime) -> Gated:
    """None when the next request may be sent as rendered now; AGAIN after a layer appended.
    A requested compaction runs first, whatever the window or the breaker says: an operator
    asked for it. Once it is answered the loop decides again, so a cancel that landed during the
    side attempt is handled before any turn request."""
    if rt.fold.compaction_request is not None:
        return await manual.requested(rt) or AGAIN
    rendered = await rt.store.render(rt.events)
    if isinstance(rendered, Err):
        return None  # the attempt renders again and reports the error
    guess = estimate(rt.events, len(rendered.value.body))
    ctx = defaults.context(rt.fold)
    turn = turn_events(rt.events)
    spent = _compacted_this_step(turn)
    breaker = breaker_open(rt.events, ctx.compact.max_failures)
    reason = _clear_reason(rt, guess)
    if reason is not None and compact.clearable(rt):
        halt = await compact.clear(rt, ctx.clear_results.keep_recent, reason)
        return halt or AGAIN
    if guess >= defaults.tokens(ctx.compact.trigger, rt.fold) and not spent and not breaker:
        halt = await compact.summarize(rt, "threshold")
        if halt is not None or not isinstance(rt.events[-1], CompactionFailedEvent):
            return halt or AGAIN
        # A failed threshold compaction doesn't end the turn: the request goes on to L4, in
        # this same step (ponytail: a crash right here resumes as context_exhausted).
        spent = True
    return await _preflight(rt, guess, blocked=spent or breaker)


async def _preflight(rt: Runtime, guess: int, *, blocked: bool) -> Gated:
    """L4: the estimate reaches W, so no request is made or billed. A cancel pending ends the
    turn cancelled, never context_exhausted: the cancellation step is next."""
    if open_cancel(rt.events) is not None:
        return AGAIN
    window = defaults.effective_window(rt.fold)
    if guess < window:
        return None
    action = "fail" if blocked else "compact"
    data = {"estimated_tokens": guess, "window_tokens": window, "action": action}
    drafts = [draft("context_preflight_blocked", data)]
    if action == "fail":
        drafts.append(draft("turn_completed", {"reason": "context_exhausted"}))
        return await append(rt, drafts)
    gated = await append(rt, drafts)
    if gated != AGAIN:
        return gated
    # The step's one reactive compaction; the request is then built fresh.
    halt = await compact.reactive(rt)
    return halt or AGAIN


def _clear_reason(rt: Runtime, guess: int) -> str | None:
    ctx = defaults.context(rt.fold).clear_results
    if guess >= defaults.tokens(ctx.trigger, rt.fold):
        return "threshold"
    idle = ctx.idle_ms
    last = next((e for e in reversed(rt.events) if isinstance(e, ModelResponseEvent)), None)
    if isinstance(idle, int) and last is not None and rt.clock() - last.time >= idle:
        return "idle"
    return None


def _compacted_this_step(turn: Sequence[Event]) -> bool:
    """The once-per-step guard L2, L4 and L5 share: a compaction or a preflight block since the
    turn's latest turn request."""
    since = 0
    for index, event in enumerate(turn):
        if isinstance(event, ModelRequestEvent) and event.data.purpose != "compaction":
            since = index + 1
    done = (CompactedEvent, CompactionFailedEvent, ContextPreflightBlockedEvent)
    return any(isinstance(e, done) for e in turn[since:])


def breaker_open(events: Sequence[Event], max_failures: int) -> bool:
    """compaction_failed since the last compacted reach max_failures."""
    failures = 0
    for event in reversed(events):
        if isinstance(event, CompactedEvent):
            break
        failures += isinstance(event, CompactionFailedEvent)
    return failures >= max_failures
