"""The `--agent` module of the simulated-user CLI tests (spec lane 32, E): agents, a judge, a budget
and the `user` model that plays the customer. Everything is scripted."""

from pydantic import BaseModel, JsonValue

from threads import Agent, RunContext, agent, scripted_model, tool
from threads.log import Budget, Permissions

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}


def use(name: str, input: dict[str, JsonValue], call_id: str) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": input}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


def say(text: str) -> JsonValue:
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "usage": USAGE}


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


def _turn(n: int) -> list[JsonValue]:
    """One turn: look the order up, then answer. A call id is used once per thread."""
    return [
        use("lookup_order", {"id": "42"}, f"r{n}"),
        say("Order 42 shipped; it is inside the 30-day window."),
    ]


TURNS: list[JsonValue] = [*_turn(0), *_turn(1), *_turn(2)]


def support() -> Agent[None, str]:
    return agent(
        name="support",
        instructions="You answer order questions.",
        model=scripted_model({"responses": TURNS}),
        tools=[LOOKUP],
        permissions=ALLOW,
    )


agents = [support()]
verdict: JsonValue = {"criterion": 1, "pass": True, "reason": "It quotes the window."}
judge = scripted_model({"responses": [use("final_output", {"verdicts": [verdict]}, "v1")]})
"""Plays the customer: one follow-up, then done."""
user = scripted_model(
    {
        "responses": [
            use("final_output", {"message": "What about a repair?", "done": False}, "u1"),
            use("final_output", {"message": "", "done": True}, "u2"),
        ]
    }
)
budget = Budget(max_model_requests=20)
