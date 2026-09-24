"""Nothing is sent after a cancel barrier, and the turn it asks to stop ends cancelled: through
an automatic or a requested compaction's fallback, and through a reactive compaction after
prompt_too_long whose summary leaks (C5)."""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import pytest
from cancel_kit import TAIL, Cancel, CancelAt, after_cancel, context, ended_cancelled, said
from compact_kit import logged

from threads import agent, sqlite
from threads.agents.context import RunContext
from threads.hooks.extension import Extension, extension
from threads.hooks.types import CompactGate
from threads.log import (
    Budget,
    CompactionFailedEvent,
    Context,
    Event,
    EventId,
    ModelRequestEvent,
    ParseError,
    TurnCompletedEvent,
)
from threads.loop import attempt, compact, ladder
from threads.loop.budget import Reservation
from threads.loop.defaults import CONTEXT
from threads.loop.model import ModelInfo, Rejected
from threads.loop.runtime import Failed, Halt, Runtime
from threads.loop.scripted import Entry
from threads.reduce.state import ReducedState
from threads.render import Rendered
from threads.result import Err, Ok
from threads.secrets import credential
from threads.store import Draft, SqliteStore
from threads.store.budgets import BudgetLedger, Cover, LimitName, Refused
from threads.thread.control import LOCAL_OPERATOR

TOO_LONG = Rejected("prompt_too_long", 400)


@dataclass(frozen=True, slots=True)
class Second:
    """How the second run is set up: its context, a leak at the cancelling send, a compaction
    request first, the test's own cancel (when the model doesn't cancel), extensions."""

    ctx: Context = CONTEXT
    leak: str | None = None
    requested: bool = False
    cancel: Cancel | None = None
    extensions: Sequence[Extension] = ()


async def _second_run(
    entries: Sequence[Entry], at: int, how: Second | None = None
) -> Sequence[Event]:
    """A first run, then (after a compaction request, with `requested`) a second whose send
    number `at` has it cancelled (0: the model never does; the test's `cancel` does)."""
    how = how or Second()
    cancel = how.cancel or Cancel()
    store = sqlite(":memory:")
    model = CancelAt(entries, at, cancel, how.leak)
    bot = agent(model=model, context=how.ctx, extensions=list(how.extensions))
    first = await bot.run("hi", store=store)
    if how.requested:
        assert isinstance(await first.thread.compact(LOCAL_OPERATOR), Ok)
    cancel.thread = first.thread
    await bot.run("next", store=store, thread=first.thread)
    return await logged(first.thread)


def test_an_automatic_compaction_fallback_is_never_sent_after_the_barrier() -> None:
    entries = [said(reported=1_000), TOO_LONG, said("summary"), said(), said()]
    events = asyncio.run(_second_run(entries, 2, Second(ctx=context(trigger={"tokens": 500}))))
    assert after_cancel(events) == TAIL
    ended_cancelled(events)


def test_a_requested_compaction_fallback_is_never_sent_after_the_barrier() -> None:
    entries = [said(), TOO_LONG, said("summary"), said(), said()]
    events = asyncio.run(_second_run(entries, 2, Second(requested=True)))
    assert after_cancel(events) == TAIL
    ended_cancelled(events)
    failed = next(e for e in events if isinstance(e, CompactionFailedEvent))
    assert failed.data.reason == "prompt_too_long"


def test_a_reactive_compaction_after_prompt_too_long_yields_to_the_cancel() -> None:
    key = credential("fake", "api_key", "sk-l09-l5-1a2b", "U")()
    entries = [said(), TOO_LONG, said("never recorded"), said(), said()]
    ctx = context(trigger={"tokens": 10_000_000})
    events = asyncio.run(_second_run(entries, 3, Second(ctx=ctx, leak=key)))
    assert after_cancel(events) == TAIL
    ended_cancelled(events)


def _nothing_sent_after_the_barrier(events: Sequence[Event]) -> None:
    names = [e.type for e in events]
    barrier = names.index("cancel_requested")
    rest = events[barrier:]
    assert not any(isinstance(e, ModelRequestEvent) for e in rest), names[barrier:]
    assert "compaction_failed" in names[barrier:]
    ended_cancelled(events)


def _cancels_then_proceeds(cancel: Cancel) -> Extension:
    async def gate(_state: ReducedState, _ctx: RunContext[None]) -> CompactGate:
        await cancel()
        return {"decision": "proceed"}

    return extension(name="ops", hooks={"before_compact": gate})


def test_before_compact_cancel_then_proceed_sends_no_automatic_summary() -> None:
    cancel = Cancel()
    entries = [said(reported=1_000), said("summary"), said(), said()]
    ctx = context(trigger={"tokens": 500})
    hook = _cancels_then_proceeds(cancel)
    run = _second_run(entries, 0, Second(ctx=ctx, cancel=cancel, extensions=[hook]))
    _nothing_sent_after_the_barrier(asyncio.run(run))


def test_before_compact_cancel_then_proceed_sends_no_requested_summary() -> None:
    cancel = Cancel()
    entries = [said(), said("summary"), said(), said()]
    hook = _cancels_then_proceeds(cancel)
    run = _second_run(entries, 0, Second(requested=True, cancel=cancel, extensions=[hook]))
    _nothing_sent_after_the_barrier(asyncio.run(run))


def _cancel_at_fallback(monkeypatch: pytest.MonkeyPatch, cancel: Cancel) -> None:
    """A cancel lands while the fallback clears old results."""
    clear = compact.clear

    async def clearing(rt: Runtime, keep_recent: int, reason: str) -> Halt | None:
        if reason == "compaction_fallback":
            await cancel()
        return await clear(rt, keep_recent, reason)

    monkeypatch.setattr(compact, "clear", clearing)


def test_a_cancel_while_the_automatic_fallback_clears_sends_nothing_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancel = Cancel()
    _cancel_at_fallback(monkeypatch, cancel)
    entries = [said(reported=1_000), TOO_LONG, said("summary"), said(), said()]
    run = _second_run(entries, 0, Second(ctx=context(trigger={"tokens": 500}), cancel=cancel))
    _nothing_sent_after_the_barrier(asyncio.run(run))


def test_a_cancel_while_the_requested_fallback_clears_sends_nothing_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancel = Cancel()
    _cancel_at_fallback(monkeypatch, cancel)
    entries = [said(), TOO_LONG, said("summary"), said(), said()]
    run = _second_run(entries, 0, Second(requested=True, cancel=cancel))
    _nothing_sent_after_the_barrier(asyncio.run(run))


def test_a_cancel_while_the_request_is_prepared_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the barrier check and before the request is recorded (its budget reservation
    awaits the store): the check in the request's own append holds."""
    cancel = Cancel()
    reserve = attempt.budget.reserve
    once: list[None] = [None]

    async def reserving(rt: Runtime, answer: Draft | None) -> Failed | Reservation:
        if once and answer is not None:
            once.pop()
            await cancel()
        return await reserve(rt, answer)

    monkeypatch.setattr(attempt.budget, "reserve", reserving)
    entries = [said(reported=1_000), said("summary"), said(), said()]
    run = _second_run(entries, 0, Second(ctx=context(trigger={"tokens": 500}), cancel=cancel))
    _nothing_sent_after_the_barrier(asyncio.run(run))


def _ends_cancelled_with_nothing_else(events: Sequence[Event], also: str) -> None:
    """The turn ends cancelled; no other turn end, and `also` recorded after the barrier."""
    names = [e.type for e in events]
    rest = events[names.index("cancel_requested") :]
    ends = [e.data.reason for e in rest if isinstance(e, TurnCompletedEvent)]
    assert ends == ["cancelled"], names
    assert also in [e.type for e in rest]


def test_a_cancel_while_the_request_renders_overtakes_a_capability_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The capability pre-check ends the turn error; with a cancel landed while the request
    rendered, the turn ends cancelled instead."""
    cancel = Cancel()
    render = SqliteStore.render
    once: list[None] = [None]

    async def rendering(
        self: SqliteStore,
        events: Sequence[Event],
        *,
        compaction: bool = False,
        cause: EventId | None = None,
    ) -> Ok[Rendered] | Err[ParseError]:
        # Armed once the second run's cancel has a thread: its request renders first.
        if once and cancel.thread is not None:
            once.pop()
            await cancel()
        return await render(self, events, compaction=compaction, cause=cause)

    async def no_ladder(_rt: Runtime) -> None:
        return None

    monkeypatch.setattr(SqliteStore, "render", rendering)
    monkeypatch.setattr(ladder, "fit", no_ladder)

    def refusing(_body: bytes, _info: ModelInfo) -> str:
        return "content_unsupported"

    monkeypatch.setattr(attempt, "mismatch", refusing)
    events = asyncio.run(_second_run([said(), said()], 0, Second(cancel=cancel)))
    _ends_cancelled_with_nothing_else(events, "cancelled")


def test_a_cancel_while_the_budget_refuses_overtakes_budget_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancel = Cancel()
    reserve = BudgetLedger.reserve

    async def reserving(
        self: BudgetLedger,
        attempt_key: str,
        covers: Sequence[Cover],
        amounts: Mapping[LimitName, int | None],
    ) -> Refused | None:
        refused = await reserve(self, attempt_key, covers, amounts)
        if refused is not None:
            await cancel()
        return refused

    monkeypatch.setattr(BudgetLedger, "reserve", reserving)

    async def main() -> Sequence[Event]:
        store = sqlite(":memory:")
        bot = agent(model=CancelAt([said(), said(), said()], 0, cancel))
        first = await bot.run("hi", store=store)
        # The summary takes the run's one request; the turn request is then refused.
        assert isinstance(await first.thread.compact(LOCAL_OPERATOR), Ok)
        cancel.thread = first.thread
        one = Budget.model_validate({"max_model_requests": 1})
        await bot.run("next", store=store, thread=first.thread, budget=one)
        return await logged(first.thread)

    _ends_cancelled_with_nothing_else(asyncio.run(main()), "budget_exceeded")
