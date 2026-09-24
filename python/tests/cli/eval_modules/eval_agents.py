"""The `--agent` module of the `threads eval` tests: a support agent, a scripted judge and a budget.
Everything is scripted: no test reaches a real model."""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager

from pydantic import BaseModel, JsonValue

from threads import Agent, RunContext, agent, scripted_model, tool
from threads.agents.bindings import AppTool, Fence
from threads.log import Budget, Permissions

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}


def use(name: str, input: dict[str, JsonValue], call_id: str) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": input}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


def say(text: str) -> JsonValue:
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "usage": USAGE}


TURN: tuple[JsonValue, ...] = (
    use("lookup_order", {"id": "42"}, "c1"),
    say("Order 42 shipped; it is inside the 30-day window."),
)


class Order(BaseModel):
    id: str


async def _lookup(args: Order, _ctx: RunContext[None]) -> str:
    return f"order {args.id}: shipped"


LOOKUP = tool(
    name="lookup_order",
    description="Look up an order by id.",
    input=Order,
    runs="host",
    effect="read_only",
    execute=_lookup,
)
ALLOW = Permissions(
    mode="default",
    allow=["lookup_order"],
    ask=[],
    deny=[],
    protected_paths=[],
    allow_bypass=False,
    plan_exit_mode="default",
)
CONNECTS = [0]
"""Connection attempts on the MCP server below: a dry pin makes none."""


class Jira:
    """An MCP server that refuses every connection."""

    name = "jira"

    @asynccontextmanager
    async def connect(self, fence: Fence) -> AsyncGenerator[Sequence[AppTool[object]]]:
        assert fence is not None
        CONNECTS[0] += 1
        raise ConnectionRefusedError("connection refused")
        yield ()


def support(*, with_mcp: bool = False) -> Agent[None, str]:
    model = scripted_model({"responses": list(TURN)})
    if with_mcp:
        return agent(
            name="support",
            instructions="You answer order questions.",
            model=model,
            tools=[LOOKUP, Jira()],
            permissions=ALLOW,
        )
    return agent(
        name="support",
        instructions="You answer order questions.",
        model=model,
        tools=[LOOKUP],
        permissions=ALLOW,
    )


agents = [support()]
verdict: JsonValue = {"criterion": 1, "pass": True, "reason": "It quotes the window."}
judge = scripted_model({"responses": [use("final_output", {"verdicts": [verdict]}, "v1")]})
budget = Budget(max_model_requests=10)
