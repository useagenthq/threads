"""Observers (F6.5): committed events after append, in log order, with a durable
cursor; a failing observer never touches the log or the run, and is retried from its cursor."""

import asyncio

from pydantic import JsonValue

from threads import Completed, agent, scripted_model, sqlite
from threads.hooks.extension import extension
from threads.log import Event
from threads.result import Ok

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
SETTLE_S = 0.05
"""Observers lag: time for the pump to catch up after a run returns."""


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def test_observers_see_each_event_once_across_runs_and_a_failure_is_retried() -> None:
    seen: list[int] = []
    failures = [True]

    async def watch(event: Event) -> None:
        if event.type == "model_response" and failures:
            failures.pop()
            raise RuntimeError("sink down")
        seen.append(event.seq)

    ext = extension(name="audit", on={"*": watch})

    async def main() -> None:
        store = sqlite(":memory:")
        bot = agent(model=scripted_model({"responses": [text("a"), text("b")]}), extensions=[ext])
        first = await bot.run("one", store=store, deps=None)
        assert isinstance(first, Completed)
        await asyncio.sleep(SETTLE_S)
        stopped_at = len(seen)
        second = await bot.run("two", store=store, thread=first.thread, deps=None)
        assert isinstance(second, Completed)
        await asyncio.sleep(SETTLE_S)
        timeline = await second.thread.timeline()
        assert isinstance(timeline, Ok)
        head = timeline.value.entries[-1].event.seq
        # The failed event was retried from the cursor, and nothing was delivered twice.
        assert seen == sorted(set(seen))
        assert seen == list(range(1, head + 1))
        assert stopped_at < head
        # The observer's failure left no trace in the log.
        assert all(e.event.type != "hook_decision" for e in timeline.value.entries)

    asyncio.run(main())
