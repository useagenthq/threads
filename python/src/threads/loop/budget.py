"""Budget reservation before every model request.

The next attempt's model-declared bound is reserved before its `model_request` exists: when
settled cost (unknown usage at its bound) plus that reservation would pass a limit,
`budget_exceeded` is recorded and no request is made, so no response can overshoot. The check
and the append run under the branch's single writer, which serializes them.

ponytail: covers this thread's own run and thread budgets, max_cost_nanos and
max_model_requests. The tree-wide `budget_ledger` (descendants reserving concurrently) comes with
subagents.
"""

from collections.abc import Sequence

from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    Budget,
    Event,
    ModelRequestEvent,
    SettingsChangedEvent,
    ThreadStartedEvent,
    UserInputEvent,
)
from threads.loop.drafts import draft
from threads.loop.runtime import Halt, Runtime, lost
from threads.reduce.fold import Fold, policy
from threads.reduce.projections import bound, dispositions
from threads.result import Err


def _next_bound(fold: Fold) -> int | None:
    pinned = policy(fold)
    if pinned is None or pinned.models is MISSING:
        return None
    started = next(e for e in fold.events if isinstance(e, ThreadStartedEvent)).data
    settings = next(
        (e.data.settings for e in reversed(fold.events) if isinstance(e, SettingsChangedEvent)),
        None,
    )
    ref = started.model if settings is None else settings.model
    params = started.model_params if settings is None else settings.model_params
    model = next(
        (m for m in pinned.models if (m.provider, m.name) == (ref.provider, ref.name)), None
    )
    return bound(model, params.get("max_tokens"), MISSING)


def _scopes(events: Sequence[Event], fold: Fold) -> list[tuple[str, Budget, int]]:
    """(scope, budget, first seq it covers): the run's budget rides on its user_input."""
    out: list[tuple[str, Budget, int]] = []
    pinned = policy(fold)
    if pinned is not None and pinned.budget is not MISSING:
        out.append(("thread", pinned.budget, 0))
    run = next((e for e in reversed(events) if isinstance(e, UserInputEvent)), None)
    if run is not None and run.data.budget is not MISSING:
        out.append(("run", run.data.budget, run.seq))
    return out


def _exceeded(fold: Fold, budget: Budget, since: int) -> dict[str, JsonValue] | None:
    requests = sum(1 for e in fold.events if isinstance(e, ModelRequestEvent) and e.seq > since)
    if budget.max_model_requests is not MISSING and requests + 1 > budget.max_model_requests:
        limit = budget.max_model_requests
        return _data("max_model_requests", limit, requests + 1, upper=False)
    if budget.max_cost_nanos is not MISSING:
        spent = [(k, u) for seq, k, u in dispositions(fold) if seq > since]
        reserve = _next_bound(fold)
        upper = sum(k if u is None else u for k, u in spent) + (reserve or 0)
        if upper > budget.max_cost_nanos:
            is_bound = reserve is not None or any(u != k for k, u in spent)
            return _data("max_cost_nanos", budget.max_cost_nanos, upper, upper=is_bound)
    return None


def _data(limit: str, value: int, observed: int, *, upper: bool) -> dict[str, JsonValue]:
    return {
        "limit": limit,
        "limit_value": value,
        "observed": observed,
        "observed_is_upper_bound": upper,
    }


async def reserve(rt: Runtime) -> Halt | None:
    """None when the next attempt fits every budget; else the turn ends budget_exhausted."""
    for scope, budget, since in _scopes(rt.events, rt.fold):
        over = _exceeded(rt.fold, budget, since)
        if over is not None:
            over["scope"] = scope
            done = await rt.append(
                draft("budget_exceeded", over),
                draft("turn_completed", {"reason": "budget_exhausted"}),
            )
            return lost(done.error) if isinstance(done, Err) else None
    return None
