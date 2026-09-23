"""Budget reservation before every model request, tree-wide.

A budget covers its thread and every descendant. Before a `model_request` in any thread of the
tree, the attempt's bound is reserved in the store's `budget_ledger` against every covering
budget (this thread's, its run's, and each ancestor's) in one transaction; the request is
appended only after that commits. When a reservation doesn't fit, `budget_exceeded` is recorded
and no request is made, so no response can overshoot. A settled attempt replaces its bound with
its disposition (item 7): known usage, or the bound when usage is unknown.

Turns and wall time are checked from the thread's own log instead (loop/limits.py).
"""

from collections.abc import Sequence

from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    Budget,
    Event,
    Model,
    ModelRequestEvent,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    SettingsChangedEvent,
    ThreadId,
    ThreadStartedEvent,
    Usage,
    UserInputEvent,
)
from threads.loop.covering import Covering
from threads.loop.drafts import draft
from threads.loop.runtime import Failed, Runtime, lost
from threads.reduce.fold import Fold, policy
from threads.reduce.projections import bound, dispositions
from threads.result import Err
from threads.store.budgets import Cover, LimitName

_LIMITS: tuple[LimitName, ...] = (
    "max_cost_nanos",
    "max_input_tokens",
    "max_output_tokens",
    "max_model_requests",
)


def own(thread_id: ThreadId, events: Sequence[Event]) -> list[Covering]:
    """This thread's pinned budget and the budget of the run its latest input started."""
    out: list[Covering] = []
    started = next((e for e in events if isinstance(e, ThreadStartedEvent)), None)
    pinned = None if started is None else started.data.policy
    if pinned is not None and pinned is not MISSING and pinned.budget is not MISSING:
        out.append(Covering(f"thread:{thread_id}", pinned.budget, "thread"))
    run = next((e for e in reversed(events) if isinstance(e, UserInputEvent)), None)
    if run is not None and run.data.budget is not MISSING:
        out.append(Covering(f"run:{thread_id}:{run.event_id}", run.data.budget, "run"))
    return out


def inherited(thread_id: ThreadId, fold: Fold, ancestors: Sequence[Covering]) -> list[Covering]:
    """What covers a child of this thread: every budget covering this one, as an ancestor's."""
    mine = [
        Covering(c.budget_id, c.budget, "ancestor", thread_id) for c in own(thread_id, fold.events)
    ]
    return [*mine, *ancestors]


def _cover(c: Covering) -> Cover | None:
    limits: dict[LimitName, int] = {
        n: v for n in _LIMITS if isinstance(v := getattr(c.budget, n), int)
    }
    return Cover(c.budget_id, limits) if limits else None


def _epoch(fold: Fold) -> tuple[Model | None, JsonValue]:
    """The current settings epoch's pinned model limits and its max_tokens."""
    pinned = policy(fold)
    started = next(e for e in fold.events if isinstance(e, ThreadStartedEvent)).data
    settings = next(
        (e.data.settings for e in reversed(fold.events) if isinstance(e, SettingsChangedEvent)),
        None,
    )
    ref = started.model if settings is None else settings.model
    params = started.model_params if settings is None else settings.model_params
    models: Sequence[Model] = () if pinned is None or pinned.models is MISSING else pinned.models
    model = next((m for m in models if (m.provider, m.name) == (ref.provider, ref.name)), None)
    return model, params.get("max_tokens")


def bounds(model: Model | None, max_tokens: JsonValue) -> dict[LimitName, int | None]:
    """An attempt's bound per limit; None where the model has none: no
    max_tokens, no input bound, or no price (spec/schema/README.md, Budget enforcement)."""
    window = None
    if model is not None and model.input_billing_bound == "context_window":
        window = model.context_window
    priced = model is not None and model.price is not MISSING
    return {
        "max_cost_nanos": bound(model, max_tokens, MISSING) if priced else None,
        "max_input_tokens": window,
        "max_output_tokens": max_tokens if isinstance(max_tokens, int) else None,
        "max_model_requests": 1,
    }


def unbounded(budget: Budget, model: Model, max_tokens: JsonValue) -> LimitName | None:
    """The first limit of the budget this model has no per-attempt bound for."""
    have = bounds(model, max_tokens)
    return next(
        (n for n in _LIMITS if isinstance(getattr(budget, n), int) and have[n] is None), None
    )


def _reserve(fold: Fold) -> dict[LimitName, int | None]:
    """The next attempt's bound per limit."""
    return bounds(*_epoch(fold))


def _known(amounts: dict[LimitName, int | None]) -> dict[LimitName, int]:
    """An unbounded limit covers nothing this attempt could charge: no covering budget let it
    through (reserve refuses it), so it records 0."""
    return {n: v or 0 for n, v in amounts.items()}


def _settled(fold: Fold, request: ModelRequestEvent) -> dict[LimitName, int] | None:
    """A resolved attempt's disposition; None while it awaits its response."""
    if request.event_id in fold.open_requests:
        return None
    reserve = _known(_reserve(fold))
    usage = _usage(fold.events, request)
    cost = next(((k if u is None else u) for s, k, u in dispositions(fold) if s == request.seq), 0)
    billed = usage is not None or cost > 0
    return {
        "max_cost_nanos": cost,
        "max_input_tokens": _input(usage, reserve["max_input_tokens"]) if billed else 0,
        "max_output_tokens": _output(usage, reserve["max_output_tokens"]) if billed else 0,
        "max_model_requests": 1,
    }


def _usage(events: Sequence[Event], request: ModelRequestEvent) -> Usage | None:
    for e in events:
        is_response = isinstance(e, ModelResponseEvent | ModelResponseRecoveredEvent)
        if is_response and e.data.request_event_id == request.event_id:
            return e.data.usage
    return None


def _input(usage: Usage | None, reserve: int) -> int:
    if usage is None or usage.input_tokens is None:
        return reserve
    extra = [usage.cache_read_tokens, usage.cache_write_tokens]
    if any(x is None for x in extra):
        return reserve
    return usage.input_tokens + sum(x for x in extra if isinstance(x, int))


def _output(usage: Usage | None, reserve: int) -> int:
    return reserve if usage is None or usage.output_tokens is None else usage.output_tokens


async def _sync(rt: Runtime, ancestors: Sequence[Covering]) -> None:
    """This branch's ledger rows agree with its log: a resolved attempt is settled, a reservation
    whose request never reached the log is dropped, and an attempt the ledger never saw (an
    imported log, a lost cache) is entered settled."""
    branch = rt.writer.branch_id
    known = await rt.store.budgets.attempts(branch)
    requests = {
        e.seq: e for e in rt.events if isinstance(e, ModelRequestEvent) and e.branch_id == branch
    }
    for key, reserved in known.items():
        request = requests.get(int(key.rsplit(":", 1)[1]))
        if request is None:
            await rt.store.budgets.release(key)
        elif reserved and (amounts := _settled(rt.fold, request)) is not None:
            await rt.store.budgets.settle(key, amounts)
    thread_id = rt.writer.fold.thread_id
    if thread_id is None:
        return
    for seq, request in requests.items():
        key = f"{branch}:{seq}"
        if key not in known:
            before = [e for e in rt.events if e.seq < seq]
            covers = _covers([*own(thread_id, before), *ancestors])
            amounts = _settled(rt.fold, request) or _known(_reserve(rt.fold))
            await rt.store.budgets.record(key, covers, amounts)


def _covers(covering: Sequence[Covering]) -> list[Cover]:
    return [c for c in map(_cover, covering) if c is not None]


async def reserve(rt: Runtime) -> Failed | None:
    """None when the next attempt's reservation committed; else the turn ends budget_exhausted
    and no model_request is appended."""
    await _sync(rt, rt.budgets)
    thread_id = rt.writer.fold.thread_id
    if thread_id is None:
        raise AssertionError("an acquired branch has a thread")
    covering = [*own(thread_id, rt.events), *rt.budgets]
    key = f"{rt.writer.branch_id}:{rt.fold.seq + 1}"
    refused = await rt.store.budgets.reserve(key, _covers(covering), _reserve(rt.fold))
    if refused is None:
        return None
    by = next(c for c in covering if c.budget_id == refused.budget_id)
    data: dict[str, JsonValue] = {
        "scope": by.scope,
        "limit": refused.limit,
        "limit_value": refused.limit_value,
        "observed": refused.observed,
        "observed_is_upper_bound": refused.limit != "max_model_requests",
    }
    if by.owner is not None:
        data["owner_thread_id"] = by.owner
    done = await rt.append(
        draft("budget_exceeded", data), draft("turn_completed", {"reason": "budget_exhausted"})
    )
    return lost(done.error) if isinstance(done, Err) else None


async def settle(rt: Runtime) -> None:
    """Settles this branch's resolved attempts (after a response, an abandon, or recovery)."""
    await _sync(rt, rt.budgets)
