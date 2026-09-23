"""Budgets through agent.run: a reservation that doesn't fit records
budget_exceeded and ends the turn budget_exhausted before any model_request; the ledger
settles each attempt, so a thread budget spans runs."""

import asyncio
import dataclasses

import pytest
from pydantic import BaseModel, JsonValue

from threads import (
    BudgetExhausted,
    Completed,
    ConfigError,
    RunContext,
    agent,
    scripted_model,
    sqlite,
    tool,
)
from threads.agents import run as run_module
from threads.agents.store import open_store
from threads.log import Budget, ModelRequestEvent, Permissions
from threads.loop.scripted import ScriptedModel
from threads.result import Ok

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
ALLOW = Permissions(
    mode="default",
    allow=["echo"],
    ask=[],
    deny=[],
    protected_paths=[],
    allow_bypass=False,
    plan_exit_mode="default",
)


class Echo(BaseModel):
    text: str


async def echo(args: Echo, _ctx: RunContext[None]) -> str:
    return args.text


SLOW_MS = 5_000

ECHO = tool(name="echo", description="Echo.", input=Echo, runs="host", execute=echo)


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def use() -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": "c1", "name": "echo", "input": {"text": "x"}}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


def test_a_run_budget_refuses_the_request_that_would_pass_it() -> None:
    async def main() -> None:
        bot = agent(
            model=scripted_model({"responses": [use(), text("never")]}),
            tools=[ECHO],
            permissions=ALLOW,
        )
        result = await bot.run(
            "go", store=sqlite(":memory:"), deps=None, budget=Budget(max_model_requests=1)
        )
        assert isinstance(result, BudgetExhausted)
        assert (result.budget.scope, result.budget.limit) == ("run", "max_model_requests")
        assert (result.budget.limit_value, result.budget.observed) == (1, 2)
        timeline = await result.thread.timeline()
        assert isinstance(timeline, Ok)
        kinds = [e.event.type for e in timeline.value.entries]
        assert kinds.count("model_request") == 1
        assert kinds[-2:] == ["budget_exceeded", "turn_completed"]

    asyncio.run(main())


def test_a_thread_budget_spans_runs_and_every_attempt_is_settled() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        bot = agent(
            model=scripted_model({"responses": [text("one"), text("two")]}),
            budget=Budget(max_model_requests=1),
        )
        first = await bot.run("a", store=store)
        assert isinstance(first, Completed)
        second = await bot.run("b", store=store, thread=first.thread)
        assert isinstance(second, BudgetExhausted)
        assert second.budget.scope == "thread"
        sq = await open_store(store)
        rows = await sq.budgets.attempts(first.thread.branch)
        timeline = await first.thread.timeline()
        assert isinstance(timeline, Ok)
        seqs = [
            e.event.seq for e in timeline.value.entries if isinstance(e.event, ModelRequestEvent)
        ]
        assert rows == {f"{first.thread.branch}:{seq}": False for seq in seqs}

    asyncio.run(main())


def test_max_turns_counts_the_thread_s_turns_across_runs() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        bot = agent(
            model=scripted_model({"responses": [text("one"), text("two")]}),
            budget=Budget(max_turns=1),
        )
        first = await bot.run("a", store=store)
        assert isinstance(first, Completed)
        second = await bot.run("b", store=store, thread=first.thread)
        assert isinstance(second, BudgetExhausted)
        assert (second.budget.scope, second.budget.limit) == ("thread", "max_turns")
        assert (second.budget.limit_value, second.budget.observed) == (1, 2)
        assert not second.budget.observed_is_upper_bound

    asyncio.run(main())


def test_max_wall_ms_refuses_a_request_after_the_run_s_time_is_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1_790_000_000_000]

    async def slow(args: Echo, _ctx: RunContext[None]) -> str:
        now[0] += SLOW_MS
        return args.text

    monkeypatch.setattr(run_module, "now_ms", lambda: now[0])
    slow_echo = tool(name="echo", description="Echo.", input=Echo, runs="host", execute=slow)

    async def main() -> None:
        bot = agent(
            model=scripted_model({"responses": [use(), text("never")]}),
            tools=[slow_echo],
            permissions=ALLOW,
        )
        result = await bot.run(
            "go", store=sqlite(":memory:"), deps=None, budget=Budget(max_wall_ms=1_000)
        )
        assert isinstance(result, BudgetExhausted)
        assert (result.budget.scope, result.budget.limit) == ("run", "max_wall_ms")
        assert result.budget.observed == SLOW_MS

    asyncio.run(main())


def _unbounded(responses: list[JsonValue]) -> ScriptedModel:
    """A model whose adapter pins no max_tokens: max_output_tokens has no per-attempt bound."""
    model = scripted_model({"responses": responses})
    model._info = dataclasses.replace(model.info, params={})  # pyright: ignore[reportPrivateUsage] - a test adapter
    return model


@pytest.mark.parametrize(
    "limit",
    [Budget(max_output_tokens=150), Budget(max_cost_nanos=1_000)],
)
def test_setup_refuses_a_limit_the_model_has_no_per_attempt_bound_for(limit: Budget) -> None:
    """spec/schema/README.md, Budget enforcement: no max_tokens, or no price, is unenforceable."""
    with pytest.raises(ConfigError) as refused:
        agent(model=_unbounded([]), budget=limit)
    assert refused.value.code == "budget_unenforceable"


def test_a_run_budget_the_tree_can_t_bound_is_refused_at_run_start() -> None:
    worker = agent(name="worker", model=_unbounded([]))
    lead = agent(model=scripted_model({"responses": []}), subagents=[worker])
    with pytest.raises(ConfigError) as refused:
        asyncio.run(lead.run("go", store=sqlite(":memory:"), budget=Budget(max_output_tokens=5)))
    assert refused.value.code == "budget_unenforceable"


def test_under_on_unknown_usage_stop_an_unbounded_attempt_is_refused_as_exceeding() -> None:
    async def main() -> None:
        bot = agent(
            model=_unbounded([text("never")]),
            budget=Budget(max_output_tokens=150),
            on_unknown_usage="stop",
        )
        result = await bot.run("go", store=sqlite(":memory:"))
        assert isinstance(result, BudgetExhausted)
        assert (result.budget.limit, result.budget.observed) == ("max_output_tokens", 0)
        assert result.budget.observed_is_upper_bound
        timeline = await result.thread.timeline()
        assert isinstance(timeline, Ok)
        assert not any(isinstance(e.event, ModelRequestEvent) for e in timeline.value.entries)

    asyncio.run(main())
