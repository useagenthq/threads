"""`deps` is optional on an agent whose tools take `RunContext[None]` (spec/api.json Agent.run
deps: "omitted, tools see null"), and required, statically, when the tools read deps."""

import asyncio
from dataclasses import dataclass

import pytest
from pydantic import BaseModel, JsonValue

from threads import (
    Agent,
    Completed,
    ConfigError,
    RunContext,
    agent,
    scripted_model,
    sqlite,
    tool,
)
from threads.log import Permissions

USAGE: JsonValue = {"input_tokens": 1, "output_tokens": 1}
ALLOW = Permissions(
    mode="default",
    allow=["look"],
    ask=[],
    deny=[],
    protected_paths=[".git/**"],
    allow_bypass=False,
    plan_exit_mode="default",
)


class Query(BaseModel):
    q: str


def _script() -> dict[str, JsonValue]:
    call: JsonValue = {"type": "tool_use", "call_id": "c1", "name": "look", "input": {"q": "x"}}
    return {
        "responses": [
            {"content": [call], "stop_reason": "tool_use", "usage": USAGE},
            {
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": USAGE,
            },
        ]
    }


def _bot(seen: list[object]) -> Agent[None, str]:
    async def look(_args: Query, ctx: RunContext[None]) -> str:
        seen.append(ctx.deps)
        return "found"

    looker = tool(name="look", description="Look.", input=Query, execute=look, effect="read_only")
    return agent(model=scripted_model(_script()), tools=[looker], permissions=ALLOW)


def test_run_without_deps_gives_tools_none() -> None:
    seen: list[object] = []
    result = asyncio.run(_bot(seen).run("go", store=sqlite(":memory:")))
    assert isinstance(result, Completed)
    assert seen == [None]


def test_stream_without_deps_gives_tools_none() -> None:
    seen: list[object] = []

    async def main() -> object:
        stream = _bot(seen).stream("go", store=sqlite(":memory:"))
        _ = [item async for item in stream]
        return await stream.result

    assert isinstance(asyncio.run(main()), Completed)
    assert seen == [None]


@dataclass(frozen=True)
class Billing:
    account: str


async def deps_are_required_when_tools_read_them(billing: Agent[Billing, str]) -> None:
    """Never called: pyright checks it. reportUnnecessaryTypeIgnoreComment fails the gate if a
    run without deps ever type-checks for an agent whose tools need them."""
    await billing.run("refund order 42")  # pyright: ignore[reportCallIssue] - deps is required
    billing.stream("refund order 42")  # pyright: ignore[reportCallIssue] - deps is required
    await billing.run("refund order 42", deps=Billing("acme"))


def test_tools_that_read_deps_still_need_them_at_runtime() -> None:
    async def charge(_args: Query, ctx: RunContext[Billing]) -> str:
        return ctx.deps.account

    charger = tool(name="look", description="Charge.", input=Query, execute=charge)
    billing = agent(model=scripted_model(_script()), tools=[charger], permissions=ALLOW)

    async def main() -> None:
        await billing.run("go", store=sqlite(":memory:"))  # pyright: ignore[reportCallIssue] - the missing deps under test

    with pytest.raises(ConfigError, match="needs deps"):
        asyncio.run(main())
