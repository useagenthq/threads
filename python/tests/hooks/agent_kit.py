"""The hooks that fire outside one plain turn: compaction, a model fallback, and a subagent."""

from collections.abc import Mapping, Sequence

from each_kit import (
    OVERLOADED,
    Decision,
    HookCase,
    kinds,
    last_at,
    logged,
    normalize,
    only_decisions_differ,
    ops,
    part,
    quick,
    say,
    small,
    uses,
)
from pydantic import JsonValue

from threads import RunContext, agent, scripted_model, sqlite
from threads.hooks.extension import Extension
from threads.hooks.types import CompactGate, StopGate, SwitchGate
from threads.log import AgentFinishedData, Event, ModelSettings, ToolCallData
from threads.reduce.state import ReducedState

SMALL: Mapping[str, JsonValue] = {
    "compact": {"trigger": {"tokens": 5}, "keep_tail": {"tokens": 1}, "max_failures": 3}
}
"""A window so small the second run has to compact before its first turn request."""


async def compacted(exts: Sequence[Extension[None]]) -> tuple[str, list[Event]]:
    """Two runs on one thread, the second of which compacts; the branch's whole log."""
    bot = agent(
        model=scripted_model({"responses": [say("one"), say("the user said one"), say("two")]}),
        context=SMALL,
        extensions=list(exts),
    )
    store = sqlite(":memory:")
    first = await bot.run("hi", store=store, deps=None)
    second = await bot.run("more", store=store, thread=first.thread, deps=None)
    return second.status, await logged(second.thread)


async def fell_back(exts: Sequence[Extension[None]]) -> tuple[str, list[Event]]:
    """A turn that falls back to a second model after two overloaded answers."""
    bot = agent(
        model=scripted_model({"responses": [OVERLOADED, OVERLOADED, say("from the primary")]}),
        fallback=[small([say("from the fallback")])],
        retry=quick(fallback_after=2),
        extensions=list(exts),
    )
    result = await bot.run("go", store=sqlite(":memory:"), deps=None)
    return result.status, await logged(result.thread)


SPAWN = uses(part("call_1", "spawn_agent", {"agent": "reviewer", "prompt": "Review."}))


async def spawned(exts: Sequence[Extension[None]]) -> tuple[str, list[Event]]:
    """A lead that spawns one child, with `exts` as its extensions."""
    reviewer = agent(name="reviewer", model=scripted_model({"responses": [say("fine")]}))
    lead = agent(
        name="lead",
        model=scripted_model({"responses": [SPAWN, say("done")]}),
        subagents=[reviewer],
        extensions=list(exts),
    )
    result = await lead.run("go", store=sqlite(":memory:"), deps=None)
    return result.status, await logged(result.thread)


async def _before_compact() -> Sequence[Decision]:
    seen: list[int] = []

    async def gate(state: ReducedState, _ctx: RunContext[None]) -> CompactGate:
        seen.append(state.turns_completed)
        return {"decision": "proceed"}

    status, log = await compacted([ops({"before_compact": gate})])
    assert status == "completed"
    assert len(seen) == 1
    # The decision is durable before the summary request it gates.
    at = kinds(log).index("compacted")
    assert kinds(log)[:at].index("hook_decision") < at
    return normalize(log)


async def _after_compact() -> Sequence[Decision]:
    seen: list[int] = []

    async def note(state: ReducedState, _ctx: RunContext[None]) -> Sequence[str]:
        seen.append(state.turns_completed)
        return ["the invoice numbers still matter"]

    status, log = await compacted([ops({"after_compact": note})])
    assert status == "completed"
    assert len(seen) == 1
    # Its injections go in after the summary, before the turn's own request.
    order = kinds(log)
    assert order.index("compacted") < order.index("injected")
    assert order.index("injected") < last_at(order, "model_request")
    return normalize(log)


async def _subagent_start() -> Sequence[Decision]:
    seen: list[str] = []

    async def deny(call: ToolCallData, _ctx: RunContext[None]) -> SwitchGate:
        seen.append(f"{call.name}:{call.call_id}")
        return {"decision": "deny", "reason": "no children today"}

    status, log = await spawned([ops({"subagent_start": deny})])
    assert status == "completed"
    assert seen == ["spawn_agent:call_1"]
    # A denied spawn starts no child at all.
    assert "agent_spawned" not in kinds(log)
    return normalize(log)


async def _subagent_stop() -> Sequence[Decision]:
    seen: list[str] = []

    async def stop(finished: AgentFinishedData, _ctx: RunContext[None]) -> StopGate:
        seen.append(finished.status)
        return {"decision": "stop"}

    status, log = await spawned([ops({"subagent_stop": stop})])
    assert status == "completed"
    assert seen == ["completed"]
    assert "agent_finished" in kinds(log)
    return normalize(log)


async def _before_model_switch() -> Sequence[Decision]:
    seen: list[str] = []

    async def deny(settings: ModelSettings, _ctx: RunContext[None]) -> SwitchGate:
        seen.append(settings.model.name)
        return {"decision": "deny", "reason": "stay on the pinned model"}

    status, log = await fell_back([ops({"before_model_switch": deny})])
    assert status == "completed"
    assert seen == ["scripted-small"]
    # ADR 0020: a deny keeps the settings epoch and the attempt is retried on it.
    assert "settings_changed" not in kinds(log)
    return normalize(log)


async def _after_model_switch() -> Sequence[Decision]:
    seen: list[str] = []

    async def note(settings: ModelSettings, _ctx: RunContext[None]) -> None:
        seen.append(settings.model.name)
        raise RuntimeError("dashboard down")

    status, log = await fell_back([ops({"after_model_switch": note})])
    _plain_status, plain = await fell_back([])
    assert status == "completed"
    assert seen == ["scripted-small"]
    assert "settings_changed" in kinds(log)
    only_decisions_differ(log, plain)
    return normalize(log)


AGENT_CASES: Mapping[str, HookCase] = {
    "before_compact": _before_compact,
    "after_compact": _after_compact,
    "subagent_start": _subagent_start,
    "subagent_stop": _subagent_stop,
    "before_model_switch": _before_model_switch,
    "after_model_switch": _after_model_switch,
}
