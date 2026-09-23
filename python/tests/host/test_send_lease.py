"""A send held past the transport fence for longer than the lease lasts keeps the lease: the run
that sends renews it, so another host can't take the branch, look the send up as not sent and
send it again while it may still land (invariant 3)."""

import asyncio
from collections.abc import Mapping

import pytest
from host.test_channel_recovery import Replies, text, until, webhook

from threads import agent, scripted_model, sqlite
from threads.agents import run
from threads.host import ChannelCapabilities, DeliveryOutcome, host
from threads.log import JsonObject
from threads.loop.model import LookupResult, NotFound
from threads.memory.fence import check
from threads.store import lease


class _FinalLookup(Replies):
    """A channel whose lookup proves nothing was sent: a run that took the branch while an
    earlier send was still on its way would find nothing and send it again. With `hold`, a send
    passes the fence and then waits before it lands."""

    def __init__(self, hold: asyncio.Event | None = None) -> None:
        super().__init__(
            hold=hold, capabilities=ChannelCapabilities("final", False, False, False, False)
        )

    async def perform(
        self, op: JsonObject, effect_key: str, credentials: Mapping[str, str]
    ) -> DeliveryOutcome:
        await check()
        return await Replies.perform(self, op, effect_key, credentials)

    async def lookup(self, effect_key: str, op: JsonObject) -> LookupResult[str]:
        return NotFound()


def test_a_send_held_past_the_fence_keeps_its_lease_so_no_host_sends_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every lease lasts 300 ms here, renewed every 100 ms by the run that holds it.
    monkeypatch.setattr(lease, "TTL_MS", 300)
    monkeypatch.setattr(run, "RENEW_EVERY_S", 0.1)
    store = sqlite(":memory:")

    async def main() -> None:
        hold = asyncio.Event()
        first = _FinalLookup(hold)
        other = _FinalLookup()
        a = host(
            store=store,
            agents={
                "bot": agent(model=scripted_model({"responses": [text("hi"), text("second")]}))
            },
            channels={"fake": first},
        )
        await a.ready()
        await a.receive("fake", webhook("d1", "m1", "hello"))
        await first.sending.wait()
        # Held past the fence for several lease lifetimes, while a next message arrives at
        # another host: its run takes the branch only once no live lease holds it.
        b = host(
            store=store,
            agents={"bot": agent(model=scripted_model({"responses": [text("second")]}))},
            channels={"fake": other},
        )
        await b.ready()
        await b.receive("fake", webhook("d2", "m2", "again"))
        await asyncio.sleep(1.0)
        hold.set()
        await until(lambda: _answered(first, other))
        await a.stop()
        await b.stop()
        texts = [op["text"] for op in (*first.sent, *other.sent)]
        assert texts.count("hi") == 1

    asyncio.run(main())


async def _answered(*channels: Replies) -> bool:
    return any(op["text"] == "second" for c in channels for op in c.sent)
