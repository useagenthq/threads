"""Observers (F6.5): committed events after append, in log order, with a durable
cursor; a failing observer never touches the log or the run, and is retried from its cursor.
The pump is driven directly so the test waits on its completion, never on a sleep."""

import asyncio

from pydantic import JsonValue

from threads import Completed, agent, scripted_model, sqlite
from threads.agents.context import RunContext
from threads.agents.store import open_store
from threads.hooks.extension import Observer, extension
from threads.hooks.observers import ObserverPump
from threads.log import Budget, Event
from threads.result import Ok

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
FAILS_AT = 3


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def test_an_agent_run_delivers_every_committed_event_to_its_observer_in_order() -> None:
    seen: list[int] = []
    delivered = asyncio.Event()

    async def watch(event: Event) -> None:
        seen.append(event.seq)
        delivered.set()

    async def main() -> None:
        bot = agent(
            model=scripted_model({"responses": [text("ok")]}),
            extensions=[extension(name="audit", on={"*": watch})],
        )
        done = await bot.run("hi", store=sqlite(":memory:"), deps=None)
        timeline = await done.thread.timeline()
        assert isinstance(timeline, Ok)
        expected = [e.event.seq for e in timeline.value.entries]
        # Delivery lags the run: wait on each delivery, never a fixed sleep.
        while seen != expected:
            await asyncio.wait_for(delivered.wait(), timeout=5)
            delivered.clear()

    asyncio.run(main())


def test_a_throwing_or_stuck_observer_changes_neither_execution_nor_the_log() -> None:
    async def down(event: Event) -> None:
        raise RuntimeError("exporter down")

    async def stuck(event: Event) -> None:
        await asyncio.Event().wait()

    async def run(on: dict[str, Observer] | None) -> tuple[bool, list[str]]:
        ext = [] if on is None else [extension(name="o", on=on)]
        bot = agent(model=scripted_model({"responses": [text("ok")]}), extensions=ext)
        done = await bot.run("hi", store=sqlite(":memory:"), deps=None)
        timeline = await done.thread.timeline()
        assert isinstance(timeline, Ok)
        return isinstance(done, Completed), [e.event.type for e in timeline.value.entries]

    async def main() -> None:
        plain = await run(None)
        assert await run({"*": down}) == plain
        assert await run({"*": stuck}) == plain

    asyncio.run(main())


def test_delivery_is_in_order_resumes_from_the_durable_cursor_and_retries_a_failure() -> None:
    seen: list[int] = []
    failing = [True]

    async def watch(event: Event) -> None:
        if event.seq == FAILS_AT and failing:
            raise RuntimeError("sink down")
        seen.append(event.seq)

    async def main() -> None:
        store = sqlite(":memory:")
        done = await agent(model=scripted_model({"responses": [text("a")]})).run(
            "one", store=store, deps=None
        )
        assert isinstance(done, Completed)
        timeline = await done.thread.timeline()
        assert isinstance(timeline, Ok)
        events = [e.event for e in timeline.value.entries]
        cursors = (await open_store(store)).cursors
        branch = done.thread.branch

        pump = ObserverPump(cursors, branch, lambda: events, {"audit": {"*": watch}})
        pump.poke()
        await pump.idle()
        assert seen == [1, 2]
        assert await cursors.get("audit", branch) == FAILS_AT - 1
        failing.clear()
        # A new pump (a restart) starts from the durable cursor, not from the beginning.
        again = ObserverPump(cursors, branch, lambda: events, {"audit": {"*": watch}})
        again.poke()
        await again.idle()
        assert seen == [e.seq for e in events]
        assert await cursors.get("audit", branch) == events[-1].seq

    asyncio.run(main())


def test_notification_fires_when_a_budget_is_exceeded_as_in_typescript() -> None:
    """The notification hook sees parked, retry_scheduled, budget_exceeded and agent_finished."""
    seen: list[str] = []

    async def notify(event: Event, _ctx: RunContext[None]) -> None:
        seen.append(event.type)

    async def main() -> None:
        use: JsonValue = {
            "content": [
                {"type": "tool_use", "call_id": "c1", "name": "todo_write", "input": {"todos": []}}
            ],
            "stop_reason": "tool_use",
            "usage": USAGE,
        }
        bot = agent(
            model=scripted_model({"responses": [use, text("ok")]}),
            budget=Budget(max_model_requests=1),
            extensions=[extension(name="ops", hooks={"notification": notify})],
        )
        await bot.run("hi", store=sqlite(":memory:"), deps=None)

    asyncio.run(main())
    assert seen == ["budget_exceeded"]
