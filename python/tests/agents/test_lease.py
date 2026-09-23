"""each run owns the branch lease on its own, keeps it through slow model
and tool awaits, hands it back when it ends, and never continues a thread under another config."""

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping

import pytest
from pydantic import BaseModel, JsonValue

from threads import (
    Completed,
    ConfigError,
    Failed,
    ModelContext,
    ModelRequest,
    Parked,
    RunContext,
    agent,
    sqlite,
    tool,
)
from threads.agents import run as run_module
from threads.agents.store import open_store
from threads.log import ModelResponseEvent
from threads.loop.model import ModelChunk, ModelInfo
from threads.loop.scripted import ScriptedModel, scripted_model
from threads.result import Ok
from threads.store.lease import TTL_MS

USAGE: JsonValue = {"input_tokens": 1, "output_tokens": 1}
FIRST_RUN_SEND = 2
"""The overlap test's second send: the first run after the seed."""


def say(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def use(name: str) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": "c1", "name": name, "input": {}}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


class Waiting(ScriptedModel):
    """A scripted model that awaits `gate(n)` before its n-th reply."""

    def __init__(
        self, script: Mapping[str, JsonValue], gate: Callable[[int], Awaitable[None]]
    ) -> None:
        parsed = scripted_model(script)
        super().__init__(parsed._entries, {})
        self._gate = gate
        self._n = 0

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        self._n += 1
        await self._gate(self._n)
        async for chunk in super().send(request, context):
            yield chunk


class Clock:
    """The run's wall clock, moved by the test; the heartbeat still runs on real sleeps."""

    def __init__(self) -> None:
        self.ms = 1_790_000_000_000

    def __call__(self) -> int:
        return self.ms

    async def pass_time(self, ms: int) -> None:
        # Small steps with real yields, so the heartbeat renews between them.
        for _ in range(ms // 1_000):
            self.ms += 1_000
            await asyncio.sleep(0.005)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(run_module, "now_ms", fake)
    monkeypatch.setattr(run_module, "RENEW_EVERY_S", 0.001)
    return fake


class Empty(BaseModel):
    pass


async def _no_wait(_n: int) -> None:
    return None


def test_a_second_run_on_a_busy_branch_is_refused_and_the_first_completes() -> None:
    async def main() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def gate(n: int) -> None:
            if n == FIRST_RUN_SEND:
                entered.set()
                await release.wait()

        bot = agent(model=Waiting({"responses": [say("seed"), say("first")]}, gate))
        seed = await bot.run("seed", store=sqlite(":memory:"))
        first = asyncio.create_task(bot.run("first", thread=seed.thread))
        await entered.wait()
        second = await bot.run("overlap", thread=seed.thread)
        release.set()
        assert isinstance(second, Failed)
        assert second.error.code == "branch_busy"
        assert isinstance(await first, Completed)

    asyncio.run(main())


def test_a_model_call_longer_than_the_lease_ttl_still_commits(clock: Clock) -> None:
    async def main() -> None:
        async def gate(_n: int) -> None:
            await clock.pass_time(TTL_MS + 1_000)

        bot = agent(model=Waiting({"responses": [say("slow")]}, gate))
        assert isinstance(await bot.run("go", store=sqlite(":memory:")), Completed)

    asyncio.run(main())


def test_a_tool_call_longer_than_the_lease_ttl_still_commits(clock: Clock) -> None:
    async def main() -> None:
        async def slow(_args: Empty, _ctx: RunContext[None]) -> str:
            await clock.pass_time(TTL_MS + 1_000)
            return "waited"

        wait = tool(
            name="wait",
            description="Wait.",
            input=Empty,
            runs="host",
            effect="read_only",
            execute=slow,
        )
        model = Waiting({"responses": [use("wait"), say("done")]}, _no_wait)
        bot = agent(model=model, tools=[wait])
        assert isinstance(await bot.run("go", store=sqlite(":memory:"), deps=None), Completed)

    asyncio.run(main())


def test_a_finished_run_hands_the_lease_back() -> None:
    async def main() -> None:
        bot = agent(model=Waiting({"responses": [say("one"), say("two")]}, _no_wait))
        one = await bot.run("one", store=sqlite(":memory:"))
        assert isinstance(await bot.run("two", thread=one.thread), Completed)

    asyncio.run(main())


def test_a_run_that_raises_still_hands_the_lease_back() -> None:
    async def main() -> None:
        bot = agent(model=Waiting({"responses": [say("a"), say("b")]}, _no_wait), instructions="1")
        seed = await bot.run("seed", store=sqlite(":memory:"))
        other = agent(model=Waiting({"responses": [say("x")]}, _no_wait), instructions="2")
        with pytest.raises(ConfigError, match="another config"):
            await other.run("boom", thread=seed.thread)
        assert isinstance(await bot.run("after", thread=seed.thread), Completed)

    asyncio.run(main())


def test_a_lost_lease_fences_the_run(clock: Clock) -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        bot = agent(model=Waiting({"responses": [say("seed"), say("late")]}, _no_wait))
        seed = await bot.run("seed", store=store)
        sq = await open_store(store)

        async def gate(n: int) -> None:
            if n == 1:
                # A stalled process: the clock jumps past the TTL with no renewal in between,
                # and another executor takes the branch.
                clock.ms += TTL_MS + 1
                taken = await sq.acquire(seed.thread.branch, "intruder", clock)
                assert isinstance(taken, Ok)

        late = agent(model=Waiting({"responses": [say("late")]}, gate))
        assert isinstance(await late.run("late", thread=seed.thread), Failed)
        read = await sq.read(seed.thread.branch, clock())
        assert isinstance(read, Ok)
        lines = [event for segment in read.value.segments for event, _ in segment.events]
        responses = [e for e in lines if isinstance(e, ModelResponseEvent)]
        assert len(responses) == 1

    asyncio.run(main())


def test_continuing_with_another_tool_binding_is_refused_before_anything_runs() -> None:
    async def main() -> None:
        ran: list[str] = []

        async def old(_args: Empty, _ctx: RunContext[None]) -> str:
            return "read only"

        async def changed(_args: Empty, _ctx: RunContext[None]) -> str:
            ran.append("unguarded")
            return "side effect"

        first = agent(
            model=Waiting({"responses": [say("seed")]}, _no_wait),
            instructions="original",
            tools=[
                tool(
                    name="action",
                    description="Read data",
                    input=Empty,
                    runs="host",
                    effect="read_only",
                    execute=old,
                )
            ],
        )
        seed = await first.run("seed", store=sqlite(":memory:"), deps=None)
        swapped = agent(
            model=Waiting({"responses": [use("action"), say("done")]}, _no_wait),
            instructions="original",
            tools=[
                tool(
                    name="action",
                    description="Send data externally",
                    input=Empty,
                    runs="host",
                    effect="unguarded",
                    execute=changed,
                )
            ],
        )
        with pytest.raises(ConfigError, match="another config"):
            await swapped.run("continue", thread=seed.thread, deps=None)
        assert ran == []

    asyncio.run(main())


def test_a_parked_thread_under_another_config_dispatches_nothing() -> None:
    async def main() -> None:
        ran: list[str] = []

        async def act(_args: Empty, _ctx: RunContext[None]) -> str:
            ran.append("act")
            return "done"

        acting = tool(
            name="act",
            description="Act.",
            input=Empty,
            runs="host",
            effect="unguarded",
            execute=act,
        )
        bot = agent(model=Waiting({"responses": [use("act")]}, _no_wait), tools=[acting])
        parked = await bot.run("go", store=sqlite(":memory:"), deps=None)
        assert isinstance(parked, Parked)
        other = agent(
            model=Waiting({"responses": [say("x")]}, _no_wait),
            instructions="changed",
            tools=[acting],
        )
        with pytest.raises(ConfigError, match="another config"):
            await other.run("again", thread=parked.thread, deps=None)
        assert ran == []

    asyncio.run(main())


def test_continuing_with_another_model_is_refused() -> None:
    async def main() -> None:
        bot = agent(model=Waiting({"responses": [say("seed")]}, _no_wait))
        seed = await bot.run("seed", store=sqlite(":memory:"))

        class Other(Waiting):
            @property
            def info(self) -> ModelInfo:
                base = super().info
                return dataclasses.replace(base, model=base.model.model_copy(update={"name": "o"}))

        other = agent(model=Other({"responses": [say("x")]}, _no_wait))
        with pytest.raises(ConfigError, match="another config"):
            await other.run("again", thread=seed.thread)

    asyncio.run(main())
