"""A cancel that lands while a response is refused as leaked (C5) is still processed: the leak
records its abandonment (and a requested compaction's failure), and the cancellation step closes
the turn as cancelled."""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from compact_kit import kinds, logged
from corpus import Clock
from kit import USER, Tools, open_store, start
from pydantic import JsonValue

from threads import agent, sqlite
from threads.log import CancelRequestedEvent, Context, Event, TextPart, TurnCompletedEvent, Usage
from threads.loop import drive as drive_module
from threads.loop.defaults import CONTEXT
from threads.loop.drafts import draft
from threads.loop.drive import drive
from threads.loop.model import ModelChunk, ModelContext, ModelRequest, ModelResponse
from threads.loop.runtime import Halt, Idle, Runtime
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
    provider material holding a registered secret. `reported` is the prompt size the first
    response reports: what the next request's context estimate starts from."""

    def __init__(self, key: str, leak: int, cancel: "Cancel", reported: int = 10) -> None:
        big = ModelResponse(
            (TextPart(type="text", text="Hi."),),
            "end_turn",
            Usage(input_tokens=reported, output_tokens=2),
            None,
        )
        super().__init__([big, HI, HI, HI, HI], {})
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


def _context(**over: JsonValue) -> Context:
    """The default context policy, a one-token tail, and `over`."""
    base = CONTEXT.model_dump(mode="json")
    compact: dict[str, JsonValue] = {"keep_tail": {"tokens": 1}, "max_failures": 3}
    return Context.model_validate(
        {**base, "compact": {**compact, "trigger": over["trigger"]}}
        | {k: v for k, v in over.items() if k != "trigger"}
    )


async def _second_turn(key: str, context: Context, reported: int) -> Sequence[Event]:
    """A first turn that fills the context, then a second whose automatic summary leaks after
    a cancel."""
    cancel = Cancel()
    store = sqlite(":memory:")
    bot = agent(model=CancelThenLeak(key, 2, cancel, reported), context=context)
    first = await bot.run("hi", store=store)
    cancel.thread = first.thread
    await bot.run("next", store=store, thread=first.thread)
    return await logged(first.thread)


LEAK_THEN_CANCEL = [
    "cancel_requested",
    "model_attempt_abandoned",
    "compaction_failed",
    "cancelled",
    "turn_completed",
]


def test_a_cancel_during_a_leaked_threshold_compaction_ends_the_turn_cancelled() -> None:
    key = credential("fake", "api_key", "sk-l09-cancel-5e6f", "U")()
    small = _context(trigger={"tokens": 500})
    events = asyncio.run(_second_turn(key, small, 1_000))
    assert _after_cancel(events) == LEAK_THEN_CANCEL
    _cancelled(events)


def test_a_cancel_during_a_leaked_reactive_compaction_ends_the_turn_cancelled() -> None:
    key = credential("fake", "api_key", "sk-l09-cancel-7a8b", "U")()
    # Nothing triggers the threshold, and only 500 tokens fit: the second request preflights.
    tight = _context(trigger={"tokens": 10_000_000}, reserve_tokens=200_000 - 500)
    events = asyncio.run(_second_turn(key, tight, 1_000))
    assert kinds(events)[kinds(events).index("user_input", 2) :][:2] == [
        "user_input",
        "context_preflight_blocked",
    ]
    assert _after_cancel(events) == LEAK_THEN_CANCEL
    _cancelled(events)


class _KilledError(Exception):
    """The process stops before the loop's cancellation step."""


def _recovered(monkeypatch: pytest.MonkeyPatch, *, compaction: bool) -> Sequence[Event]:
    """The run dies right after the leak's batch; the next run on the thread recovers it."""

    async def killed(_rt: Runtime, _cancel: CancelRequestedEvent) -> Halt | None:
        raise _KilledError

    async def main() -> Sequence[Event]:
        key = credential("fake", "api_key", "sk-l09-crash-9c0d", "U")()
        cancel = Cancel()
        store = sqlite(":memory:")
        context = _context(trigger={"tokens": 500}) if compaction else None
        model = CancelThenLeak(key, 2, cancel, 1_000)
        bot = agent(model=model) if context is None else agent(model=model, context=context)
        first = await bot.run("hi", store=store)
        cancel.thread = first.thread
        with monkeypatch.context() as m:
            m.setattr(drive_module, "_cancel", killed)
            with pytest.raises(_KilledError):
                await bot.run("next", store=store, thread=first.thread)
        dead = await logged(first.thread)
        assert kinds(dead)[-1] == ("compaction_failed" if compaction else "model_attempt_abandoned")
        await bot.run("again", store=store, thread=first.thread)
        return await logged(first.thread)

    return asyncio.run(main())


def _carried_out(events: Sequence[Event]) -> None:
    names = kinds(events)
    assert not any(
        isinstance(e, TurnCompletedEvent) and e.data.reason == "interrupted" for e in events
    )
    at = names.index("cancelled")
    ended = events[at + 1]
    assert isinstance(ended, TurnCompletedEvent)
    assert ended.data.reason == "cancelled"


def test_recovery_after_the_leaked_turn_response_carries_the_cancel_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _carried_out(_recovered(monkeypatch, compaction=False))


def test_recovery_after_the_failed_automatic_compaction_carries_the_cancel_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _carried_out(_recovered(monkeypatch, compaction=True))
