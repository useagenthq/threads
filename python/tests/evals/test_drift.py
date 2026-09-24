"""Drift against real agents (spec lane 22, B.3, test 5): the case's recorded config and the agent
as it is pinned now, for free: no model call."""

import asyncio
from dataclasses import replace
from pathlib import Path

from eval_kit import ALLOW, LOOKUP, REFUND, REFUND_TURN, Order, saved, support
from pydantic import JsonValue

from threads import Agent, EvalReport, RunContext, agent, run_evals, scripted_model, tool
from threads.agents.bindings import AppTool
from threads.loop import guard
from threads.loop.model import ModelInfo
from threads.loop.scripted import ScriptedModel
from threads.reduce.handlers import to_json


async def _exchange(args: Order, _ctx: RunContext[None]) -> str:
    return f"exchanged {args.id}"


async def _nothing(_args: Order, _ctx: RunContext[None]) -> str:
    return ""


class _Params(ScriptedModel):
    """The scripted model under other generation settings: another model for drift."""

    def __init__(self, params: dict[str, JsonValue]) -> None:
        super().__init__([], {})
        self._settings = replace(super().info, params=params)

    @property
    def info(self) -> ModelInfo:
        return self._settings


def changed(
    *,
    name: str = "support",
    instructions: str = "You handle refunds.",
    tools: list[AppTool[None]] | None = None,
    params: dict[str, JsonValue] | None = None,
) -> Agent[None, str]:
    model = scripted_model({"responses": []}) if params is None else _Params(params)
    return agent(
        name=name,
        instructions=instructions,
        model=model,
        tools=tools if tools is not None else [LOOKUP, REFUND],
        permissions=ALLOW,
    )


def drift_of(
    bot: Agent[None, str], tmp: Path, *, strict: bool = False
) -> tuple[EvalReport, dict[str, JsonValue]]:
    async def body() -> EvalReport:
        if not (tmp / "refund-policy").exists():
            await saved(tmp)
        return await run_evals(cases=str(tmp), agents=[bot], strict=strict)

    report = asyncio.run(body())
    case = to_json(report.cases[0])
    assert isinstance(case, dict)
    return report, case


def test_the_recorded_config_no_drift_and_no_model_call(tmp_path: Path) -> None:
    before = guard.requests_seen()
    report, case = drift_of(support(REFUND_TURN), tmp_path)
    assert (case["status"], case["checks"]["drift"]) == ("passed", {"ok": True, "kinds": []})  # type: ignore[index] - JSON read
    assert report.summary == "1 passed, 0 failed"
    assert (report.model_calls.agent, report.model_calls.judge, guard.requests_seen()) == (
        0,
        0,
        before,
    )


def test_changed_instructions_are_prompt_drift_stale_failing_only_under_strict(
    tmp_path: Path,
) -> None:
    bot = changed(instructions="You handle refunds and exchanges.")
    report, case = drift_of(bot, tmp_path)
    assert (case["status"], case["reason"]) == ("stale", "drift: prompt")
    assert report.ok
    assert not drift_of(bot, tmp_path, strict=True)[0].ok


def test_an_added_removed_and_reshaped_tool_are_tools_drift_by_name(tmp_path: Path) -> None:
    reshaped = tool(
        name="lookup_order",
        description="Look up an order by its id.",
        input=Order,
        runs="host",
        effect="read_only",
        execute=_nothing,
    )
    exchange = tool(
        name="issue_exchange",
        description="Exchange an order.",
        input=Order,
        runs="host",
        execute=_exchange,
    )
    _, case = drift_of(changed(tools=[reshaped, exchange]), tmp_path)
    assert case["checks"]["drift"] == {  # type: ignore[index] - JSON read
        "ok": False,
        "kinds": ["tools"],
        "tools": {
            "added": ["issue_exchange"],
            "removed": ["issue_refund"],
            "changed": ["lookup_order"],
        },
    }
    assert case["reason"] == "drift: tools (+issue_exchange, -issue_refund, ~lookup_order)"


def test_another_model_is_model_drift(tmp_path: Path) -> None:
    _, case = drift_of(changed(params={"max_tokens": 2048}), tmp_path)
    assert case["checks"]["drift"]["kinds"] == ["model"]  # type: ignore[index] - JSON read


def test_a_hashed_only_change_a_concurrent_tool_is_config_drift(tmp_path: Path) -> None:
    concurrent = tool(
        name="lookup_order",
        description="Look up an order by id.",
        input=Order,
        runs="host",
        effect="read_only",
        concurrent=True,
        execute=_nothing,
    )
    _, case = drift_of(changed(tools=[concurrent, REFUND]), tmp_path)
    assert (case["status"], case["checks"]["drift"]["kinds"]) == ("stale", ["config"])  # type: ignore[index] - JSON read


def test_an_agent_the_case_names_is_missing_stale_listing_the_names_given(tmp_path: Path) -> None:
    _, case = drift_of(changed(name="billing"), tmp_path)
    assert (case["status"], case["reason"], case["checks"]["drift"]["agents"]) == (  # type: ignore[index] - JSON read
        "stale",
        "agent_not_found",
        ["billing"],
    )
