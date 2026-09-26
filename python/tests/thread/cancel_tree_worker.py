"""The other process of the tree-cancel drill (test_cancel_tree_cross_process.py): it holds both
the parent's and its child's leases. It prints `running` once the child is inside a tool that
waits, releases that tool on any stdin line, and prints `done <outcome>` when its run ends."""

import asyncio
import sys

from pydantic import BaseModel, JsonValue

from threads import agent, scripted_model, sqlite, tool
from threads.agents.run import RunContext

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def use(name: str, args: JsonValue, call_id: str) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


class Empty(BaseModel):
    pass


async def main(path: str) -> None:
    gate = asyncio.Event()

    async def waits(_args: Empty, _ctx: RunContext[None]) -> str:
        print("running", flush=True)
        await gate.wait()
        return "ok"

    slow = tool(
        name="slow",
        description="Waits.",
        input=Empty,
        runs="host",
        effect="read_only",
        execute=waits,
    )
    worker = agent(
        name="worker",
        model=scripted_model({"responses": [use("slow", {}, "w1"), text("x")]}),
        tools=[slow],
    )
    lead = agent(
        model=scripted_model(
            {
                "responses": [
                    use("spawn_agent", {"agent": "worker", "prompt": "Do it."}, "s1"),
                    text("never"),
                ]
            }
        ),
        # The lead lists `slow` too: a child only gets the tools its parent has.
        tools=[slow],
        subagents=[worker],
    )
    running = asyncio.create_task(lead.run("Go.", store=sqlite(path), deps=None))
    await asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)
    gate.set()
    result = await running
    print(f"done {type(result).__name__.lower()}", flush=True)


asyncio.run(main(sys.argv[1]))
