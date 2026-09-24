"""Runtime.append_decided: a decided batch passes the cancel barrier like every loop append, and
a refusal comes back as itself with nothing appended."""

import asyncio
from collections.abc import Sequence
from dataclasses import replace

from corpus import Clock
from kit import T0, USER, Tools, open_store, start, text

from threads.loop.drafts import draft
from threads.loop.runtime import Appended, Barred, Runtime
from threads.loop.scripted import scripted_model
from threads.result import Ok
from threads.store import Draft, StoredEvent
from threads.store.writer import Decide, Refusal

CANCEL = replace(draft("cancel_requested", {"scope": "turn"}), actor=USER)


async def _runtime() -> Runtime:
    clock = Clock(T0)
    model = scripted_model({"responses": [text("Done.")]})
    return await start(await open_store(), [], model, Tools({}, clock), clock)


def _decided(*drafts: Draft) -> Decide[str]:
    return lambda _c, _f: drafts


def test_a_decided_batch_is_appended_and_observed() -> None:
    async def main() -> tuple[Appended | Refusal[str], int]:
        rt = await _runtime()
        seen: list[int] = []

        def observe(events: Sequence[StoredEvent]) -> None:
            seen.append(len(events))

        rt = replace(rt, observe=observe)
        return await rt.append_decided(_decided(CANCEL)), sum(seen)

    done, observed = asyncio.run(main())
    assert isinstance(done, Ok)
    assert [e.type for e in done.value] == ["cancel_requested"]
    assert observed == len(done.value)


def test_a_refusal_comes_back_and_nothing_is_appended() -> None:
    async def main() -> tuple[Appended | Refusal[str], int, int]:
        rt = await _runtime()
        before = rt.fold.seq
        refused = await rt.append_decided(lambda _c, _f: Refusal("mailbox_full"))
        return refused, before, rt.fold.seq

    refused, before, after = asyncio.run(main())
    assert refused == Refusal("mailbox_full")
    assert after == before


def test_after_a_cancel_the_barrier_keeps_no_other_turn_ending() -> None:
    async def main() -> Appended | Refusal[str]:
        rt = await _runtime()
        assert isinstance(await rt.append(CANCEL), Ok)
        return await rt.append_decided(_decided(draft("turn_completed", {"reason": "end_turn"})))

    assert asyncio.run(main()) == Barred(())
