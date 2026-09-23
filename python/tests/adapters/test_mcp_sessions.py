"""Lane 09: every check() and every run opens its own MCP session and closes it before it
returns, however it ends. The stdio kit server is one process per connection and logs when each
starts and ends, so open connections are counted from the server's side."""

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import BaseModel, JsonValue

from threads import Completed, ConfigError, RunContext, agent, scripted_model, sqlite, tool
from threads.log import Permissions, ToolResultEvent
from threads.mcp import McpServer, mcp
from threads.result import Err, Ok

KIT = str(Path(__file__).resolve().parents[1] / "mcp_kit.py")
RUNS = 2
USAGE: JsonValue = {"input_tokens": 1, "output_tokens": 1}
BYPASS = Permissions.model_validate(
    {
        "mode": "bypass",
        "allow": [],
        "ask": [],
        "deny": [],
        "protected_paths": [],
        "allow_bypass": True,
        "plan_exit_mode": "default",
    }
)


def say(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def use(name: str, args: JsonValue, call_id: str = "c1") -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


@dataclass(frozen=True, slots=True)
class Kit:
    server: McpServer
    log: Path

    def _lines(self) -> list[str]:
        return self.log.read_text().split() if self.log.exists() else []

    def opened(self) -> int:
        return self._lines().count("open")

    def open(self) -> int:
        return self.opened() - self._lines().count("closed")


def kit(tmp_path: Path) -> Kit:
    log = tmp_path / "connections"
    server = mcp(name="kit", command=sys.executable, args=[KIT], env={"CONNECTIONS_FILE": str(log)})
    return Kit(server, log)


class NoInput(BaseModel):
    pass


def test_check_leaves_no_connection_open_on_success_and_on_failure(tmp_path: Path) -> None:
    k = kit(tmp_path)
    fine = agent(model=scripted_model({"responses": []}), tools=[k.server])
    assert asyncio.run(fine.check()) == Ok(None)
    assert (k.opened(), k.open()) == (1, 0)

    async def clash(_args: NoInput, _ctx: RunContext[None]) -> str:
        return "mine"

    same_name = tool(name="mcp__kit__echo", description="Clash.", input=NoInput, execute=clash)
    bot = agent(model=scripted_model({"responses": []}), tools=[same_name, k.server])
    checked = asyncio.run(bot.check())
    assert isinstance(checked, Err)
    assert checked.error.code == "duplicate_name"
    assert (k.opened(), k.open()) == (2, 0)


def test_a_run_calls_through_its_own_session_and_closes_it(tmp_path: Path) -> None:
    k = kit(tmp_path)
    script: JsonValue = {"responses": [use("mcp__kit__echo", {"text": "hi"}), say("done")]}
    bot = agent(model=scripted_model(script), tools=[k.server], permissions=BYPASS)

    async def main() -> str:
        assert await bot.check() == Ok(None)
        result = await bot.run("go", store=sqlite(":memory:"))
        assert isinstance(result, Completed)
        timeline = await result.thread.timeline()
        assert isinstance(timeline, Ok)
        (done,) = [e.event for e in timeline.value.entries if isinstance(e.event, ToolResultEvent)]
        return done.data.preview

    assert "echo: hi" in asyncio.run(main())
    assert (k.opened(), k.open()) == (2, 0)


def test_a_cancelled_run_closes_its_session(tmp_path: Path) -> None:
    k = kit(tmp_path)
    started = asyncio.Event()

    async def hang(_args: NoInput, _ctx: RunContext[None]) -> str:
        started.set()
        await asyncio.Event().wait()
        return "never"

    stuck = tool(name="hang", description="Hangs.", input=NoInput, execute=hang)
    script: JsonValue = {"responses": [use("hang", {}), say("never")]}
    bot = agent(model=scripted_model(script), tools=[stuck, k.server], permissions=BYPASS)

    async def main() -> None:
        run = asyncio.create_task(bot.run("go", store=sqlite(":memory:"), deps=None))
        await started.wait()
        assert k.open() == 1
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run

    asyncio.run(main())
    assert (k.opened(), k.open()) == (1, 0)


def test_two_concurrent_runs_of_one_agent_hold_two_sessions(tmp_path: Path) -> None:
    k = kit(tmp_path)
    seen: list[int] = []
    both = asyncio.Event()

    async def meet(_args: NoInput, _ctx: RunContext[None]) -> str:
        seen.append(k.open())
        if len(seen) == RUNS:
            both.set()
        await both.wait()
        return "met"

    meeting = tool(name="meet", description="Waits.", input=NoInput, execute=meet)
    script: JsonValue = {
        "responses": [use("meet", {}, "c1"), use("meet", {}, "c2"), say("one"), say("two")]
    }
    bot = agent(model=scripted_model(script), tools=[meeting, k.server], permissions=BYPASS)

    async def main() -> None:
        runs = await asyncio.gather(
            bot.run("a", store=sqlite(":memory:"), deps=None),
            bot.run("b", store=sqlite(":memory:"), deps=None),
        )
        assert all(isinstance(r, Completed) for r in runs)

    asyncio.run(main())
    assert seen[-1] == RUNS
    assert k.open() == 0


def test_an_unreachable_server_fails_check_as_a_value_and_the_run_as_config_error() -> None:
    gone = mcp(name="gone", command="/nonexistent/threads-mcp")
    bot = agent(model=scripted_model({"responses": []}), tools=[gone])

    async def main() -> None:
        checked = await bot.check()
        assert isinstance(checked, Err)
        assert checked.error.code == "mcp_unreachable"
        with pytest.raises(ConfigError) as raised:
            await bot.run("go", store=sqlite(":memory:"))
        assert raised.value.code == "mcp_unreachable"

    asyncio.run(main())
