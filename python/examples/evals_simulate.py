# Simulated users: save a real conversation, then grade the current agent against a user who keeps
# talking. Needs: nothing (scripted models stand in for real ones; no API keys, no network).
# Run: cd python && uv run python examples/evals_simulate.py
#
# For the live smoke, swap each scripted_model for anthropic("claude-haiku-4-5") and pass a
# conversation budget: Budget(max_input_tokens=150_000, max_output_tokens=15_000). Never in CI.
"""Save a real conversation, then grade the current agent against a simulated user."""

import asyncio
import tempfile
from collections.abc import Sequence

from pydantic import BaseModel, JsonValue

from threads import (
    Agent,
    CaseExpectation,
    Live,
    RunContext,
    agent,
    run_evals,
    scripted_model,
    sqlite,
    tool,
)
from threads.log import Budget, Permissions
from threads.result import Err

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
REFUSED = "Order 42 arrived 40 days ago, outside the 30-day refund window."
REPAIR = "A free repair is available instead; shall I book it?"
BOOKED = "Booked the repair for order 42."


def say(text: str) -> JsonValue:
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "usage": USAGE}


def call(name: str, input: dict[str, JsonValue], call_id: str) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": input}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


class Order(BaseModel):
    id: str


async def lookup(args: Order, _ctx: RunContext[None]) -> str:
    return f"order {args.id}: delivered 40 days ago"


lookup_order = tool(
    name="lookup_order",
    description="Look up an order by id.",
    input=Order,
    runs="host",
    effect="read_only",
    execute=lookup,
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


def support(ids: str, answers: Sequence[str]) -> Agent[None, str]:
    """One turn per answer: look the order up, then say it. A call id is used once per thread, and
    a continued prefix puts the recorded ids on the live thread too, so the live agent needs its
    own."""
    responses: list[JsonValue] = []
    for i, answer in enumerate(answers):
        responses.append(call("lookup_order", {"id": "42"}, f"{ids}{i}"))
        responses.append(say(answer))
    return agent(
        name="support",
        instructions="You answer refund questions. Quote the 30-day refund window.",
        model=scripted_model({"responses": responses}),
        tools=[lookup_order],
        permissions=ALLOW,
    )


def user_turn(message: str, call_id: str, *, done: bool) -> JsonValue:
    return call("final_output", {"message": message, "done": done}, call_id)


# The customer: pushes back twice, then stops. Live, this is one model call per message.
customer = scripted_model(
    {
        "responses": [
            user_turn("That seems harsh.", "u1", done=False),
            user_turn("Any other option?", "u2", done=False),
            user_turn("", "u3", done=True),
        ]
    }
)
VERDICTS: JsonValue = {
    "verdicts": [
        {"criterion": 1, "pass": True, "reason": "It quotes the window."},
        {"criterion": 2, "pass": True, "reason": "It offers the repair."},
    ]
}
judge = scripted_model({"responses": [call("final_output", VERDICTS, "v1")]})


async def main() -> str:
    cases = tempfile.mkdtemp(prefix="cases-")

    # 1. A real conversation, saved once at the turn the customer pushed back. Everything before
    #    that turn is the prefix; the turn's own text is the simulation's first user message.
    recorded = support("r", [REFUSED, REPAIR])
    store = sqlite(":memory:")
    first = await recorded.run("Can I still return order 42?", store=store)
    pushback = await recorded.run("But it only broke yesterday.", store=store, thread=first.thread)
    saved = await pushback.thread.save_case(
        "refund-pushback",
        expect=CaseExpectation(must=({"type": "tool_call", "data": {"name": "lookup_order"}},)),
        external_effects="stub",
        simulate={
            "kind": "model",
            "persona": "A customer who bought headphones 40 days ago. Polite but persistent.",
            "goal": "Get a refund, or a clear reason why not and what else is possible.",
            "max_messages": 3,
        },
        rubric=(
            "Never promises a refund outside the 30-day window",
            "Offers the repair option once the refund is refused",
        ),
        dir=cases,
    )
    if isinstance(saved, Err):
        raise RuntimeError(saved.error.message)

    # 2. Live: the current agent talks to the simulated customer, and the judge grades it all.
    graded = await run_evals(
        cases=cases,
        agents=[support("s", [REFUSED, REPAIR, BOOKED])],
        live=Live(judge, Budget(max_model_requests=40), user=customer),
    )
    return graded.summary


if __name__ == "__main__":
    print(asyncio.run(main()))
# Output: 1 passed, 0 failed; 10 model calls (6 agent, 3 user, 1 judge), cost unknown; refund-pushback: 3 messages, user done
