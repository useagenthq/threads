"""A store outage is the host's to wait out, never the model's to resend: one met while a model
streams fails the run, which the host's recovery runs on with backoff (as in TypeScript)."""

import asyncio
import logging
from collections.abc import AsyncIterator

import pytest
from host.test_api_recovery import ALICE, fold, has, start, text, until
from pydantic import JsonValue

from threads import agent, scripted_model, sqlite
from threads.host import host, reopen
from threads.log import ModelAttemptAbandonedEvent, TurnCompletedEvent
from threads.loop.model import Delta, ModelChunk, ModelContext, ModelRequest
from threads.loop.scripted import ScriptedModel
from threads.store import StoreError


class Blinks(ScriptedModel):
    """A scripted model whose first stream meets a store outage after its first delta."""

    def __init__(self, *replies: JsonValue) -> None:
        super().__init__(scripted_model({"responses": list(replies)})._entries, {})
        self.sends = 0

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        self.sends += 1
        if self.sends == 1:
            yield Delta("partial")
            raise StoreError("disk I/O error")
        async for chunk in super().send(request, context):
            yield chunk


def test_met_mid_stream_the_run_fails_and_the_host_runs_it_on_after_a_wait(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(reopen, "REOPEN_S", 0.02)

    async def main() -> None:
        store = sqlite(":memory:")
        model = Blinks(text("done"), text("done"))
        with caplog.at_level(logging.WARNING, logger="threads"):
            async with host(store=store, agents={"support": agent(model=model)}) as served:
                run = await start(served, ALICE)
                await until(has(store, ALICE, run, TurnCompletedEvent))
        events = (await fold(store, ALICE, run)).events
        reasons = [e.data.reason for e in events if isinstance(e, ModelAttemptAbandonedEvent)]
        assert "stream_broken" not in reasons
        assert isinstance(events[-1], TurnCompletedEvent)
        said = [r.getMessage() for r in caplog.records if "store error" in r.getMessage()]
        assert len(said) == 1

    asyncio.run(main())
