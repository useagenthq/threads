"""stop() aborts first: runs are cancelled before intake in flight is waited on, so a consumer
waiting on its run never holds shutdown up, and a send that never answers stays begun (in doubt,
for the next start to reconcile), never assumed unsent. The recovery seam waits only on the runs
its pass started, not on unrelated live runs."""

import asyncio
import json
from collections.abc import Mapping, Sequence

from host.test_channel_recovery import (
    TEAM,
    USER,
    Replies,
    _consumed,  # pyright: ignore[reportPrivateUsage] - the shared probe
    crashed,
    text,
    until,
    webhook,
)

from threads import agent, extension, scripted_model, sqlite
from threads.agents.context import RunContext
from threads.agents.store import Store, open_store, scoped
from threads.host import DeliveryOutcome, RawRequest, host
from threads.host.app import recovered
from threads.log import EffectBeginEvent, Event, JsonObject
from threads.result import Ok

STOP_S = 2.0


async def _events(store: Store) -> list[Event]:
    sq = await open_store(scoped(store, TEAM))
    rows = await sq.tables.inbox_rows()
    root = await sq.root(rows[0].thread_id)
    if not isinstance(root, Ok):
        return []
    read = await sq.read(root.value, 0)
    return list(read.value.fold.events) if isinstance(read, Ok) else []


def test_stop_does_not_wait_on_a_consumer_whose_run_has_not_recorded_its_input() -> None:
    store = sqlite(":memory:")
    entered = asyncio.Event()

    async def held(_source: object, _ctx: RunContext[None]) -> Sequence[str]:
        entered.set()
        await asyncio.Event().wait()
        return ()

    async def main() -> None:
        bot = agent(
            model=scripted_model({"responses": [text("hi")]}),
            extensions=[extension(name="held", hooks={"session_start": held})],
        )
        served = host(store=store, agents={"bot": bot}, channels={"fake": Replies()})
        await served.ready()
        await served.receive("fake", webhook("d1", "m1", "hello"))
        await entered.wait()
        await asyncio.wait_for(served.stop(), STOP_S)
        # Never recorded: the item waits for the next start.
        assert not await _consumed(store)

    asyncio.run(main())


def test_stop_does_not_wait_on_a_send_that_never_answers() -> None:
    store = sqlite(":memory:")

    async def main() -> None:
        channel = Replies(hold=asyncio.Event())
        bot = agent(model=scripted_model({"responses": [text("hi")]}))
        served = host(store=store, agents={"bot": bot}, channels={"fake": channel})
        await served.ready()
        await served.receive("fake", webhook("d1", "m1", "hello"))
        await channel.sending.wait()
        await asyncio.wait_for(served.stop(), STOP_S)
        events = await _events(store)
        # Begun and unsettled: potentially sent, never assumed unsent.
        assert any(isinstance(e, EffectBeginEvent) for e in events)
        assert not [e for e in events if e.type in ("effect_resolved", "effect_commit")]

    asyncio.run(main())


class _HoldsC2(Replies):
    """Sends to C1 go at once; a send to C2 waits for as long as the test runs."""

    async def perform(
        self, op: JsonObject, effect_key: str, credentials: Mapping[str, str]
    ) -> DeliveryOutcome:
        if op["address"] == "C2":
            self.sending.set()
            await asyncio.Event().wait()
        return await Replies.perform(self, op, effect_key, credentials)


def _to_c2(delivery: str) -> RawRequest:
    item = {
        "kind": "message",
        "principal": USER.model_dump(),
        "address": "C2",
        "item_key": f"{delivery}#0",
        "content": "hello",
    }
    return RawRequest({"delivery": delivery}, json.dumps([item]).encode())


def test_the_recovery_seam_does_not_wait_on_an_unrelated_live_run() -> None:
    store = sqlite(":memory:")

    async def main() -> None:
        await crashed(store)  # C1's reply is owed
        channel = _HoldsC2()
        bot = agent(model=scripted_model({"responses": [text("C2 reply")]}))
        served = host(store=store, agents={"bot": bot}, channels={"fake": channel})
        await served.ready()
        await served.receive("fake", _to_c2("d2"))
        await channel.sending.wait()
        # C2's run is live and never ends; C1's recovery is what the seam waits on.
        await asyncio.wait_for(recovered(served), STOP_S)
        await until(lambda: _sent_to(channel, "C1"))
        await asyncio.wait_for(served.stop(), STOP_S)

    asyncio.run(main())


async def _sent_to(channel: Replies, address: str) -> bool:
    return any(op["address"] == address for op in channel.sent)
