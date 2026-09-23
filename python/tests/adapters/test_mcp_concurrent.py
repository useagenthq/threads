"""Parallel tool calls: an MCP tool is never concurrent, so it runs alone between two concurrent
app reads (spec/schema/README.md, Parallel tool calls)."""

import asyncio
import sys
from pathlib import Path

from pydantic import BaseModel, JsonValue

from threads import Completed, RunContext, Tool, agent, scripted_model, sqlite, tool
from threads.log import Permissions
from threads.mcp import mcp

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
KIT = str(Path(__file__).resolve().parents[1] / "mcp_kit.py")
ALLOW = Permissions(
    mode="default",
    allow=["mcp__kit__echo"],
    ask=[],
    deny=[],
    protected_paths=[],
    allow_bypass=False,
    plan_exit_mode="default",
)


class Query(BaseModel):
    pass


def test_an_mcp_call_between_two_concurrent_reads_runs_alone() -> None:
    trace: list[str] = []

    def traced(name: str) -> Tool[Query, str, None]:
        async def run(_args: Query, _ctx: RunContext[None]) -> str:
            trace.append(f"start {name}")
            await asyncio.sleep(0)
            trace.append(f"end {name}")
            return name

        return tool(
            name=name,
            description=f"The {name} tool.",
            input=Query,
            effect="read_only",
            concurrent=True,
            execute=run,
        )

    parts: list[JsonValue] = [
        {"type": "tool_use", "call_id": "call_1", "name": "a", "input": {}},
        {"type": "tool_use", "call_id": "call_2", "name": "mcp__kit__echo", "input": {"text": "x"}},
        {"type": "tool_use", "call_id": "call_3", "name": "b", "input": {}},
    ]
    done: JsonValue = {
        "content": [{"type": "text", "text": "Done."}],
        "stop_reason": "end_turn",
        "usage": USAGE,
    }
    response: JsonValue = {"content": parts, "stop_reason": "tool_use", "usage": USAGE}

    async def main() -> None:
        kit = mcp(name="kit", command=sys.executable, args=[KIT], tools={"allow": ["echo"]})
        bot = agent(
            model=scripted_model({"responses": [response, done]}),
            tools=[traced("a"), kit, traced("b")],
            permissions=ALLOW,
        )
        assert bot.definition.concurrent_tools() == {"a", "b"}
        result = await bot.run("go", store=sqlite(":memory:"), deps=None)
        assert isinstance(result, Completed), result

    asyncio.run(main())
    assert trace.index("end a") < trace.index("start b")
