"""Nothing is sent after a cancel barrier, and the turn it asks to stop ends cancelled: through
an automatic or a requested compaction's fallback, and through a reactive compaction after
prompt_too_long whose summary leaks (C5)."""

import asyncio
from collections.abc import Sequence

from cancel_kit import TAIL, Cancel, CancelAt, after_cancel, context, ended_cancelled, said
from compact_kit import logged

from threads import agent, sqlite
from threads.log import Context, Event
from threads.loop.model import Rejected
from threads.loop.scripted import Entry
from threads.result import Ok
from threads.secrets import credential
from threads.thread.control import LOCAL_OPERATOR

TOO_LONG = Rejected("prompt_too_long", 400)


async def _second_run(
    entries: Sequence[Entry],
    at: int,
    ctx: Context | None = None,
    *,
    leak: str | None = None,
    requested: bool = False,
) -> Sequence[Event]:
    """A first run, then (after a compaction request, with `requested`) a second whose send
    number `at` has it cancelled."""
    cancel = Cancel()
    store = sqlite(":memory:")
    model = CancelAt(entries, at, cancel, leak)
    bot = agent(model=model) if ctx is None else agent(model=model, context=ctx)
    first = await bot.run("hi", store=store)
    if requested:
        assert isinstance(await first.thread.compact(LOCAL_OPERATOR), Ok)
    cancel.thread = first.thread
    await bot.run("next", store=store, thread=first.thread)
    return await logged(first.thread)


def test_an_automatic_compaction_fallback_is_never_sent_after_the_barrier() -> None:
    entries = [said(reported=1_000), TOO_LONG, said("summary"), said(), said()]
    events = asyncio.run(_second_run(entries, 2, context(trigger={"tokens": 500})))
    assert after_cancel(events) == TAIL
    ended_cancelled(events)


def test_a_requested_compaction_fallback_is_never_sent_after_the_barrier() -> None:
    entries = [said(), TOO_LONG, said("summary"), said(), said()]
    events = asyncio.run(_second_run(entries, 2, requested=True))
    assert after_cancel(events) == TAIL
    ended_cancelled(events)


def test_a_reactive_compaction_after_prompt_too_long_yields_to_the_cancel() -> None:
    key = credential("fake", "api_key", "sk-l09-l5-1a2b", "U")()
    entries = [said(), TOO_LONG, said("never recorded"), said(), said()]
    ctx = context(trigger={"tokens": 10_000_000})
    events = asyncio.run(_second_run(entries, 3, ctx, leak=key))
    assert after_cancel(events) == TAIL
    ended_cancelled(events)
