"""A cancel that lands while a response is refused as leaked (C5) is still processed: the leak
records its abandonment (and a requested compaction's failure), and the cancellation step closes
the turn as cancelled."""

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING

import pytest
from cancel_kit import TAIL, Cancel, CancelAt, after_cancel, context, ended_cancelled, said
from compact_kit import kinds, logged
from corpus import Clock
from kit import Tools, open_store, start

from threads import agent, sqlite
from threads.agents.store import now_ms
from threads.agents.store import open_store as open_store_of
from threads.log import CancelRequestedEvent, Context, Event, TurnCompletedEvent
from threads.loop import drive as drive_module
from threads.loop.drive import drive
from threads.loop.runtime import Halt, Idle, Runtime
from threads.result import Ok
from threads.secrets import credential
from threads.store import Draft
from threads.thread.control import LOCAL_OPERATOR

if TYPE_CHECKING:
    from threads.thread.handle import Thread


def leaking(key: str, leak: int, cancel: Cancel, reported: int = 10) -> CancelAt:
    """A script whose first response reports `reported` prompt tokens; send number `leak` has
    the run cancelled, then refuses its response as leaked."""
    return CancelAt([said(reported=reported), *[said()] * 4], leak, cancel, key)


def test_a_cancel_during_a_leaked_turn_response_ends_the_turn_cancelled() -> None:
    async def main() -> tuple[object, Sequence[Event]]:
        key = credential("fake", "api_key", "sk-l09-cancel-1a2b", "U")()
        cancel = Cancel()
        model = leaking(key, 1, cancel)
        clock = Clock(1_790_000_000_000)
        rt = await start(await open_store(), [], model, Tools({}, clock), clock)
        cancel.rt = rt
        return await drive(rt), rt.events

    halt, events = asyncio.run(main())
    assert halt == Idle("cancelled")
    assert after_cancel(events) == [
        "cancel_requested",
        "model_attempt_abandoned",
        "cancelled",
        "turn_completed",
    ]
    ended_cancelled(events)


def test_a_cancel_during_a_leaked_requested_summary_ends_the_turn_cancelled() -> None:
    async def main() -> Sequence[Event]:
        key = credential("fake", "api_key", "sk-l09-cancel-3c4d", "U")()
        cancel = Cancel()
        store = sqlite(":memory:")
        bot = agent(model=leaking(key, 2, cancel))
        first = await bot.run("hi", store=store)
        assert isinstance(await first.thread.compact(LOCAL_OPERATOR), Ok)
        cancel.thread = first.thread
        await bot.run("next", store=store, thread=first.thread)
        return await logged(first.thread)

    events = asyncio.run(main())
    assert after_cancel(events) == [
        "cancel_requested",
        "model_attempt_abandoned",
        "compaction_failed",
        "cancelled",
        "turn_completed",
    ]
    ended_cancelled(events)


async def _second_turn(key: str, context: Context, reported: int) -> Sequence[Event]:
    """A first turn that fills the context, then a second whose automatic summary leaks after
    a cancel."""
    cancel = Cancel()
    store = sqlite(":memory:")
    bot = agent(model=leaking(key, 2, cancel, reported), context=context)
    first = await bot.run("hi", store=store)
    cancel.thread = first.thread
    await bot.run("next", store=store, thread=first.thread)
    return await logged(first.thread)


def test_a_cancel_during_a_leaked_threshold_compaction_ends_the_turn_cancelled() -> None:
    key = credential("fake", "api_key", "sk-l09-cancel-5e6f", "U")()
    small = context(trigger={"tokens": 500})
    events = asyncio.run(_second_turn(key, small, 1_000))
    assert after_cancel(events) == TAIL
    ended_cancelled(events)


def test_a_cancel_during_a_leaked_reactive_compaction_ends_the_turn_cancelled() -> None:
    key = credential("fake", "api_key", "sk-l09-cancel-7a8b", "U")()
    # Nothing triggers the threshold, and only 500 tokens fit: the second request preflights.
    tight = context(trigger={"tokens": 10_000_000}, reserve_tokens=200_000 - 500)
    events = asyncio.run(_second_turn(key, tight, 1_000))
    assert kinds(events)[kinds(events).index("user_input", 2) :][:2] == [
        "user_input",
        "context_preflight_blocked",
    ]
    assert after_cancel(events) == TAIL
    ended_cancelled(events)


class _KilledError(Exception):
    """The process stops before the loop's cancellation step."""


def _recovered(
    monkeypatch: pytest.MonkeyPatch, *, compaction: bool, torn: bool = False
) -> Sequence[Event]:
    """The run dies right after the leak's batch; the next run on the thread recovers it. With
    `torn`, an older writer recorded `cancelled` in between but not its turn_completed."""

    async def killed(_rt: Runtime, _cancel: CancelRequestedEvent) -> Halt | None:
        raise _KilledError

    async def main() -> Sequence[Event]:
        key = credential("fake", "api_key", "sk-l09-crash-9c0d", "U")()
        cancel = Cancel()
        store = sqlite(":memory:")
        ctx = context(trigger={"tokens": 500}) if compaction else None
        model = leaking(key, 2, cancel, 1_000)
        bot = agent(model=model) if ctx is None else agent(model=model, context=ctx)
        first = await bot.run("hi", store=store)
        cancel.thread = first.thread
        with monkeypatch.context() as m:
            m.setattr(drive_module, "_cancel", killed)
            with pytest.raises(_KilledError):
                await bot.run("next", store=store, thread=first.thread)
        dead = await logged(first.thread)
        assert kinds(dead)[-1] == ("compaction_failed" if compaction else "model_attempt_abandoned")
        if torn:
            await _half_cancelled(first.thread, dead)
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


async def _half_cancelled(thread: "Thread", events: Sequence[Event]) -> None:
    """What an older TypeScript writer could leave: `cancelled` without its turn_completed."""
    barrier = next(e for e in reversed(events) if isinstance(e, CancelRequestedEvent))
    sq = await open_store_of(thread.store)
    writer = await sq.acquire(thread.branch, "older", now_ms)
    assert isinstance(writer, Ok)
    done = await writer.value.append([Draft("cancelled", {"request_event_id": barrier.event_id})])
    assert isinstance(done, Ok)
    await writer.value.release()


def test_recovery_of_a_half_recorded_cancellation_ends_the_turn_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _recovered(monkeypatch, compaction=False, torn=True)
    assert not any(
        isinstance(e, TurnCompletedEvent) and e.data.reason == "interrupted" for e in events
    )
    names = kinds(events)
    at = names.index("cancelled")
    assert names[at + 1] == "turn_completed"
    ended = events[at + 1]
    assert isinstance(ended, TurnCompletedEvent)
    assert ended.data.reason == "cancelled"
