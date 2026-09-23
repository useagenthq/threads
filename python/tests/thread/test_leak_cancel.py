"""A cancel that lands while a response is refused as leaked (C5) is still processed: the leak
records its abandonment (and a requested compaction's failure), and the cancellation step closes
the turn as cancelled."""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from compact_kit import kinds, logged
from corpus import Clock
from kit import USER, Tools, open_store, start

from threads import agent, sqlite
from threads.log import Event, TextPart, TurnCompletedEvent, Usage
from threads.loop.drafts import draft
from threads.loop.drive import drive
from threads.loop.model import ModelChunk, ModelContext, ModelRequest, ModelResponse
from threads.loop.runtime import Idle, Runtime
from threads.loop.scripted import ScriptedModel
from threads.result import Ok
from threads.secrets import credential
from threads.thread.control import LOCAL_OPERATOR

if TYPE_CHECKING:
    from threads.thread.handle import Thread

USAGE = Usage(input_tokens=10, output_tokens=2)
HI = ModelResponse((TextPart(type="text", text="Hi."),), "end_turn", USAGE, None)


class CancelThenLeak(ScriptedModel):
    """Plays the script, but on send number `leak` it first has the run cancelled, then stores
    provider material holding a registered secret."""

    def __init__(self, key: str, leak: int, cancel: "Cancel") -> None:
        super().__init__([HI, HI], {})
        self.key, self.leak, self.cancel, self.sends = key, leak, cancel, 0

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        self.sends += 1
        if self.sends == self.leak:
            await self.cancel()
            await context.put(json.dumps({"encrypted": self.key}).encode(), "application/json")
        async for chunk in super().send(request, context):
            yield chunk


class Cancel:
    """Appends the cancel through whatever the test has at hand once the run is live."""

    def __init__(self) -> None:
        self.rt: Runtime | None = None
        self.thread: Thread | None = None

    async def __call__(self) -> None:
        if self.rt is not None:
            barrier = replace(draft("cancel_requested", {"scope": "turn"}), actor=USER)
            assert isinstance(await self.rt.append(barrier), Ok)
        elif self.thread is not None:
            assert isinstance(await self.thread.cancel(LOCAL_OPERATOR), Ok)


def _after_cancel(events: Sequence[Event]) -> list[str]:
    names = kinds(events)
    return names[names.index("cancel_requested") :]


def _cancelled(events: Sequence[Event]) -> None:
    ended = events[-1]
    assert isinstance(ended, TurnCompletedEvent)
    assert ended.data.reason == "cancelled"


def test_a_cancel_during_a_leaked_turn_response_ends_the_turn_cancelled() -> None:
    async def main() -> tuple[object, Sequence[Event]]:
        key = credential("fake", "api_key", "sk-l09-cancel-1a2b", "U")()
        cancel = Cancel()
        model = CancelThenLeak(key, 1, cancel)
        clock = Clock(1_790_000_000_000)
        rt = await start(await open_store(), [], model, Tools({}, clock), clock)
        cancel.rt = rt
        return await drive(rt), rt.events

    halt, events = asyncio.run(main())
    assert halt == Idle("cancelled")
    assert _after_cancel(events) == [
        "cancel_requested",
        "model_attempt_abandoned",
        "cancelled",
        "turn_completed",
    ]
    _cancelled(events)


def test_a_cancel_during_a_leaked_requested_summary_ends_the_turn_cancelled() -> None:
    async def main() -> Sequence[Event]:
        key = credential("fake", "api_key", "sk-l09-cancel-3c4d", "U")()
        cancel = Cancel()
        store = sqlite(":memory:")
        bot = agent(model=CancelThenLeak(key, 2, cancel))
        first = await bot.run("hi", store=store)
        assert isinstance(await first.thread.compact(LOCAL_OPERATOR), Ok)
        cancel.thread = first.thread
        await bot.run("next", store=store, thread=first.thread)
        return await logged(first.thread)

    events = asyncio.run(main())
    assert _after_cancel(events) == [
        "cancel_requested",
        "model_attempt_abandoned",
        "compaction_failed",
        "cancelled",
        "turn_completed",
    ]
    _cancelled(events)
