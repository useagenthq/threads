"""Turn and wall-time limits: checked from this thread's own log before each
turn request. They bound the thread and its run, not the tree, so they need no ledger."""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import Budget, Event, TurnCompletedEvent, UserInputEvent
from threads.loop.drafts import draft
from threads.loop.runtime import Failed, Runtime, lost
from threads.reduce.fold import policy
from threads.result import Err

if TYPE_CHECKING:
    from pydantic import JsonValue


def _over(
    budget: Budget, events: Sequence[Event], since: int, now: int
) -> tuple[str, int, int] | None:
    """The first of max_turns / max_wall_ms the next request would pass, observed from the
    window's events (started at `since`)."""
    turns = sum(isinstance(e, TurnCompletedEvent) for e in events) + 1
    for limit, observed in (("max_turns", turns), ("max_wall_ms", now - since)):
        cap = getattr(budget, limit)
        if isinstance(cap, int) and observed > cap:
            return limit, cap, observed
    return None


async def check(rt: Runtime) -> Failed | None:
    """None when the thread's and its run's turn and wall-time limits allow the next turn
    request; else the turn ends budget_exhausted."""
    events, now = rt.events, rt.clock()
    pinned = policy(rt.fold)
    at = next(
        (i for i in range(len(events) - 1, -1, -1) if isinstance(events[i], UserInputEvent)), None
    )
    windows: list[tuple[str, Budget, Sequence[Event], int]] = []
    if pinned is not None and pinned.budget is not MISSING and events:
        windows.append(("thread", pinned.budget, events, events[0].time))
    run = None if at is None else events[at]
    if isinstance(run, UserInputEvent) and run.data.budget is not MISSING:
        windows.append(("run", run.data.budget, events[at:], run.time))
    for scope, budget, window, since in windows:
        over = _over(budget, window, since, now)
        if over is not None:
            return await _exceeded(rt, scope, over)
    return None


async def _exceeded(rt: Runtime, scope: str, over: tuple[str, int, int]) -> Failed | None:
    limit, cap, observed = over
    data: dict[str, JsonValue] = {
        "scope": scope,
        "limit": limit,
        "limit_value": cap,
        "observed": observed,
        "observed_is_upper_bound": False,
    }
    # The run ends before its ladder: an unanswered compaction request is answered here, in the
    # same batch, so it never outlives the run that should have carried it out.
    request = rt.fold.compaction_request
    answered = (
        ()
        if request is None
        else (
            draft(
                "compaction_failed",
                {"stage": "summary", "reason": "model_error", "cause_event_id": request.event_id},
            ),
        )
    )
    done = await rt.append(
        draft("budget_exceeded", data),
        *answered,
        draft("turn_completed", {"reason": "budget_exhausted"}),
    )
    return lost(done.error) if isinstance(done, Err) else None
