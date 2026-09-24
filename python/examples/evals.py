# Easy evals: save a real turn as a case, then check it the way CI does, for free.
# Needs: nothing (a scripted model stands in for a real one; no API keys, no network).
# Run: cd python && uv run python examples/evals.py
"""Save a real turn as a case, then check it the way CI does, for free."""

import asyncio
import tempfile

from pydantic import BaseModel, JsonValue

from threads import (
    CaseExpectation,
    RunContext,
    agent,
    run_evals,
    scripted_model,
    sqlite,
    tool,
)
from threads.log import Permissions
from threads.result import Err

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}


class Order(BaseModel):
    id: str


async def lookup(args: Order, _ctx: RunContext[None]) -> str:
    return f"order {args.id}: delivered 12 days ago"


lookup_order = tool(
    name="lookup_order",
    description="Look up an order by id.",
    input=Order,
    runs="host",
    effect="read_only",
    execute=lookup,
)

use: JsonValue = {
    "type": "tool_use",
    "call_id": "c1",
    "name": "lookup_order",
    "input": {"id": "42"},
}
answer: JsonValue = {
    "type": "text",
    "text": "Order 42 arrived 12 days ago, inside the 30-day refund window.",
}
support = agent(
    name="support",
    instructions="You answer refund questions. Quote the 30-day refund window.",
    model=scripted_model(
        {
            "responses": [
                {"content": [use], "stop_reason": "tool_use", "usage": USAGE},
                {"content": [answer], "stop_reason": "end_turn", "usage": USAGE},
            ]
        }
    ),
    tools=[lookup_order],
    permissions=Permissions(
        mode="default",
        allow=["lookup_order"],
        ask=[],
        deny=[],
        protected_paths=[],
        allow_bypass=False,
        plan_exit_mode="default",
    ),
)


async def main() -> str:
    cases = tempfile.mkdtemp(prefix="cases-")

    # 1. A real turn you liked, saved once as a case.
    result = await support.run("Can I still return order 42?", store=sqlite(":memory:"))
    saved = await result.thread.save_case(
        "refund-window",
        expect=CaseExpectation(must=({"type": "tool_call", "data": {"name": "lookup_order"}},)),
        external_effects="stub",
        rubric=("Quotes the 30-day refund window",),
        dir=cases,
    )
    if isinstance(saved, Err):
        raise RuntimeError(saved.error.message)

    # 2. In CI, for free: replay, rerun and drift against the agent as it is now.
    report = await run_evals(cases=cases, agents=[support])
    return report.summary


if __name__ == "__main__":
    print(asyncio.run(main()))
# Output: 1 passed, 0 failed
