"""Budget reservation before every model request, tree-wide.

A budget covers its thread and every descendant. Before a `model_request` in any thread of the
tree, the attempt's bound is reserved in the store's `budget_ledger` against every covering
budget (this thread's, its run's, and each ancestor's) in one transaction; the request is
appended only after that commits. When a reservation doesn't fit, `budget_exceeded` is recorded
and no request is made, so no response can overshoot. A settled attempt replaces its bound with
its disposition: known usage, or the bound when usage is unknown.

Turns and wall time are checked from the thread's own log instead (loop/limits.py).
"""

from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    Budget,
    Event,
    MessageReceivedEvent,
    Model,
    ModelRef,
    ModelRequestEvent,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    Policy,
    ThreadId,
    ThreadStartedEvent,
    Usage,
    UserInputEvent,
)
from threads.loop import epoch
from threads.loop.covering import Covering
from threads.loop.drafts import draft
from threads.loop.runtime import Failed, Runtime, lost
from threads.loop.team_runtime import TeamAgentPin, TeamRecipient
from threads.reduce.fold import Fold
from threads.reduce.openers import run_opener
from threads.reduce.projections import bound, dispositions, output_bound
from threads.result import Err
from threads.store import Draft
from threads.store.budgets import BudgetLedger, Cover, LimitName

_LIMITS: tuple[LimitName, ...] = (
    "max_cost_nanos",
    "max_input_tokens",
    "max_output_tokens",
    "max_model_requests",
)


def own(thread_id: ThreadId, events: Sequence[Event]) -> list[Covering]:
    """This thread's pinned budget and the budget of the run its last turn belongs to."""
    out: list[Covering] = []
    started = next((e for e in events if isinstance(e, ThreadStartedEvent)), None)
    pinned = None if started is None else started.data.policy
    if pinned is not None and pinned is not MISSING and pinned.budget is not MISSING:
        out.append(Covering(f"thread:{thread_id}", pinned.budget, "thread"))
    run = _run_input(thread_id, events)
    if run is not None and run.data.budget is not MISSING:
        out.append(Covering(f"run:{thread_id}:{run.event_id}", run.data.budget, "run"))
    return out


def _run_input(thread_id: ThreadId, events: Sequence[Event]) -> UserInputEvent | None:
    """The user_input of the run the last turn belongs to (spec/schema/README.md, "Which run a
    turn is charged to"): its opener's own, or, for a turn a receipt opened, the request its
    provenance names when that request is in this thread. Before any turn, the latest input's."""
    opener = run_opener(events)
    if opener is None:
        return next((e for e in reversed(events) if isinstance(e, UserInputEvent)), None)
    if isinstance(opener, MessageReceivedEvent):
        root = opener.data.envelope.provenance.root_request
        if root.thread_id != thread_id:
            return None
        opener = next((e for e in events if e.event_id == root.event_id), None)
    return opener if isinstance(opener, UserInputEvent) else None


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
        "max_output_tokens": output_bound(max_tokens),
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
    return bounds(epoch.limits(fold), epoch.current(fold).model_params.get("max_tokens"))


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
    imported log, a lost cache) is entered settled, against every budget that covered it, a team
    member's run budget included."""
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
            run = await _run_of(rt, before)
            covers = _covers([*own(thread_id, before), *run, *ancestors])
            amounts = _settled(rt.fold, request) or _known(_reserve(rt.fold))
            await rt.store.budgets.record(key, covers, amounts)


def _covers(covering: Sequence[Covering]) -> list[Cover]:
    return [c for c in map(_cover, covering) if c is not None]


type Reservation = Literal["reserved", "refused"]
"""Reserved: the attempt may be sent. Refused: budget_exceeded (and `answer`) is recorded, and
the turn ended budget_exhausted, or a pending cancel's step ends it."""


async def reserve(rt: Runtime, answer: Draft | None = None) -> Failed | Reservation:
    """The next attempt's reservation. When a covering budget refuses it, no model_request is
    appended: budget_exceeded, `answer` (a side request's compaction_failed) and the turn's end
    are one batch."""
    await _sync(rt, rt.budgets)
    thread_id = rt.writer.fold.thread_id
    if thread_id is None:
        raise AssertionError("an acquired branch has a thread")
    covering = await covering_of(rt)
    key = f"{rt.writer.branch_id}:{rt.fold.seq + 1}"
    refused = await rt.store.budgets.reserve(key, _covers(covering), _reserve(rt.fold))
    if refused is None:
        return "reserved"
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
    answered = () if answer is None else (answer,)
    done = await rt.append(
        draft("budget_exceeded", data),
        *answered,
        draft("turn_completed", {"reason": "budget_exhausted"}),
    )
    return lost(done.error) if isinstance(done, Err) else "refused"


async def settle(rt: Runtime) -> None:
    """Settles this branch's resolved attempts (after a response, an abandon, or recovery)."""
    await _sync(rt, rt.budgets)


async def covering_of(rt: Runtime) -> list[Covering]:
    """Every budget covering this thread: its own, then (a team member's turn) the run budget of
    the request its turn belongs to, then its ancestors' (spec/schema/README.md, "Teams")."""
    thread_id = rt.writer.fold.thread_id
    if thread_id is None:
        raise AssertionError("an acquired branch has a thread")
    return [*own(thread_id, rt.events), *await _run_of(rt, rt.events), *rt.budgets]


async def _run_of(rt: Runtime, events: Sequence[Event]) -> list[Covering]:
    """A team member's turn, the last of `events`: the run budget of the request it belongs to."""
    team, opener = rt.team, run_opener(events)
    if team is None or team.run_covering is None or opener is None:
        return []
    found = await team.run_covering(opener)
    return [] if found is None else [found]


async def inherited_by(rt: Runtime) -> list[Covering]:
    """What covers a subagent of this thread: every budget covering this one, a member's run
    budget too, as an ancestor's."""
    thread_id = rt.writer.fold.thread_id
    if thread_id is None:
        raise AssertionError("an acquired branch has a thread")
    return [
        c if c.scope == "ancestor" else Covering(c.budget_id, c.budget, "ancestor", thread_id)
        for c in await covering_of(rt)
    ]


async def room_for(rt: Runtime, member: TeamAgentPin, cap: Budget | None = None) -> bool:
    """start's headroom: every budget that would cover the new member (the starter's, the
    member's own and the start's cap) has room for one request of its model. A limit it can't
    bound has none."""
    return await start_room(rt.store.budgets, await covering_of(rt), member, cap)


async def start_room(
    budgets: BudgetLedger,
    starter: Sequence[Covering],
    member: TeamAgentPin,
    cap: Budget | None = None,
) -> bool:
    """Whether `starter` (every budget covering whoever starts the member), the member's own
    budget and the start's cap have room for one request of its model. An operator start's
    starter is the lead."""
    covering = list(starter)
    if member.budget is not None:
        covering.append(Covering("member", member.budget, "thread"))
    if cap is not None:
        covering.append(Covering("start", cap, "thread"))
    return await _fits(budgets, covering, member.policy, member.model, member.params)


async def room_in(rt: Runtime, to: TeamRecipient) -> bool:
    """ask's headroom: the recipient's own and ancestors' budgets, and the run budget of the
    asking turn's request (its turn belongs to that request), have room for one request of its
    model."""
    run = [c for c in await covering_of(rt) if c.scope == "run"]
    return await _fits(rt.store.budgets, [*to.covering, *run], to.policy, to.model, to.params)


async def ask_room(budgets: BudgetLedger, to: TeamRecipient) -> bool:
    """An operator's ask's headroom: the recipient's own and ancestors' budgets have room for one
    request of its model (a Phase 1 operator request has no run budget of its own)."""
    return await _fits(budgets, to.covering, to.policy, to.model, to.params)


async def _fits(
    budgets: BudgetLedger,
    covering: Sequence[Covering],
    policy: Policy | None,
    ref: ModelRef,
    params: Mapping[str, JsonValue],
) -> bool:
    """Every limit of `covering` has room for one request of `ref` (a new member's own budget
    starts empty). A limit the model can't bound has none."""
    models: Sequence[Model] = () if policy is None or policy.models is MISSING else policy.models
    model = next((m for m in models if (m.provider, m.name) == (ref.provider, ref.name)), None)
    amounts = bounds(model, params.get("max_tokens"))
    for c in covering:
        for limit in _LIMITS:
            most = getattr(c.budget, limit)
            if not isinstance(most, int):
                continue
            amount = amounts[limit]
            spent = 0 if c.budget_id == "member" else await budgets.spent(c.budget_id, limit)
            if amount is None or spent + amount > most:
                return False
    return True
