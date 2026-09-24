"""What a host retries when it recovers an API run: only a store error, which the store raises as
`StoreError`, backing off while it lasts. A provider's lookup failing is the loop's to settle (a
model request becomes unknown and is sent again under the loop's own budget); the host never
retries it."""

import asyncio
import logging
from collections.abc import AsyncIterator

import pytest
from host.test_api_recovery import ALICE, expire_leases, fold, has, start, text, until
from pydantic import JsonValue

from threads import Store, agent, scripted_model, sqlite
from threads.agents import run as run_module
from threads.host import Host, host, reopen
from threads.host.app import recovered
from threads.log import ModelAttemptAbandonedEvent, ModelRequestEvent, TurnCompletedEvent
from threads.loop.model import (
    LookupResult,
    ModelChunk,
    ModelContext,
    ModelRequest,
    ModelResponse,
)
from threads.loop.scripted import ScriptedModel
from threads.store import SqliteStore, StoreError

FEWEST, MOST = 3, 14
"""Store failures seen in 0.6 s while backing off from 10 ms to at most 80 ms."""


class Lookups(ScriptedModel):
    """A scripted model with response lookup. `held` keeps its requests unanswered (its host is
    as good as dead); `refuse` makes each lookup fail as a refused connection."""

    def __init__(self, *replies: JsonValue, held: bool = False, refuse: bool = False) -> None:
        answers: dict[str, JsonValue] = {"none": {}}
        super().__init__(scripted_model({"responses": list(replies)})._entries, answers)
        self.held = asyncio.Event()
        if not held:
            self.held.set()
        self.refuse = refuse
        self.lookups = 0

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        await self.held.wait()
        async for chunk in super().send(request, context):
            yield chunk

    async def lookup(self, request_id: str, context: ModelContext) -> LookupResult[ModelResponse]:
        self.lookups += 1
        if self.refuse:
            raise ConnectionRefusedError("provider down")
        return await super().lookup(request_id, context)


def test_a_provider_lookup_that_raises_is_settled_by_the_loop_never_retried_by_the_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reopen, "REOPEN_S", 0.02)

    async def main() -> None:
        store = sqlite(":memory:")
        late = Lookups(text("late"), held=True)
        first = support_of(store, late)
        run = await start(first, ALICE)
        await until(has(store, ALICE, run, ModelRequestEvent))
        await expire_leases(store)

        model = Lookups(text("done"), refuse=True)
        async with support_of(store, model) as second:
            await recovered(second)
            await asyncio.sleep(0.2)
        events = (await fold(store, ALICE, run)).events
        assert model.lookups == 1
        abandoned = [e for e in events if isinstance(e, ModelAttemptAbandonedEvent)]
        assert [e.data.provider_outcome for e in abandoned] == ["unknown"]
        assert isinstance(events[-1], TurnCompletedEvent)
        late.held.set()
        await first.stop()

    asyncio.run(main())


def test_a_lasting_store_error_backs_off_is_said_once_and_the_run_goes_on_when_it_passes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(reopen, "REOPEN_S", 0.01)
    monkeypatch.setattr(reopen, "STORE_BACKOFF_MAX_S", 0.08)

    async def main() -> None:
        store = sqlite(":memory:")
        late = Lookups(text("late"), held=True)
        first = support_of(store, late)
        run = await start(first, ALICE)
        await until(has(store, ALICE, run, ModelRequestEvent))

        second = support_of(store, Lookups(text("done")))
        async with second:
            # The first pass loses to the live lease; from then on every run's store fails.
            await recovered(second)
            opened = run_module.open_store
            attempts = 0
            failing = True

            async def broken(given: Store) -> SqliteStore:
                nonlocal attempts
                if not failing:
                    return await opened(given)
                attempts += 1
                raise StoreError("disk I/O error")

            monkeypatch.setattr(run_module, "open_store", broken)
            await expire_leases(store)
            with caplog.at_level(logging.WARNING, logger="threads"):
                await asyncio.sleep(0.6)
            # Once every 10 ms would be about 60; backing off to 80 ms it is about 10.
            assert FEWEST <= attempts <= MOST
            said = [r.getMessage() for r in caplog.records if "store error" in r.getMessage()]
            assert len(said) == 1
            failing = False
            await until(has(store, ALICE, run, TurnCompletedEvent))
        late.held.set()
        await first.stop()

    asyncio.run(main())


def support_of(store: Store, model: Lookups) -> Host:
    return host(store=store, agents={"support": agent(model=model)})
