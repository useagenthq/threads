"""A reply the channel refused before anything was sent is sent again under the same key when the
refusal is rate_limited or transient, up to 3 attempts in total, as in TypeScript; a permanent
refusal is recorded as not sent (spec/api.json DeliveryOutcome)."""

import asyncio
from collections.abc import Mapping

from host.test_channel_recovery import Replies, text, until, webhook

from threads import agent, scripted_model, sqlite
from threads.agents.store import Store, open_store, scoped
from threads.host import DeliveryError, DeliveryOutcome, host
from threads.log import EffectBeginEvent, JsonObject, ToolResultEvent
from threads.loop.effects import MAX_SENDS
from threads.result import Ok


class _Refusing(Replies):
    """Refuses the first `refusals` sends with `kind`, then sends."""

    def __init__(self, kind: str, refusals: int) -> None:
        super().__init__()
        self.kind = kind
        self.refusals = refusals

    async def perform(
        self, op: JsonObject, effect_key: str, credentials: Mapping[str, str]
    ) -> DeliveryOutcome:
        if self.refusals > 0:
            self.refusals -= 1
            kind = "permanent" if self.kind == "permanent" else "rate_limited"
            return DeliveryError(kind, "definite_not_sent")
        return await Replies.perform(self, op, effect_key, credentials)


async def _log(store: Store) -> tuple[int, list[str]]:
    sq = await open_store(scoped(store, "T1"))
    rows = await sq.tables.inbox_rows()
    root = await sq.root(rows[0].thread_id)
    read = None if not isinstance(root, Ok) else await sq.read(root.value, 0)
    if not isinstance(read, Ok):
        return 0, []  # the thread isn't made yet
    events = read.value.fold.events
    begins = sum(isinstance(e, EffectBeginEvent) for e in events)
    results = [e.data.preview for e in events if isinstance(e, ToolResultEvent)]
    return begins, results


def _run(channel: _Refusing) -> tuple[int, list[str]]:
    store = sqlite(":memory:")

    async def main() -> tuple[int, list[str]]:
        served = host(
            store=store,
            agents={"bot": agent(model=scripted_model({"responses": [text("hi")]}))},
            channels={"fake": channel},
        )
        async with served:
            await served.receive("fake", webhook("d1", "m1", "hello"))

            async def settled() -> bool:
                return len((await _log(store))[1]) > 0

            await until(settled)
        return await _log(store)

    return asyncio.run(main())


def test_a_rate_limited_reply_is_sent_again_under_the_same_key() -> None:
    channel = _Refusing("rate_limited", 2)
    begins, results = _run(channel)
    assert [op["text"] for op in channel.sent] == ["hi"]
    assert begins == MAX_SENDS
    assert results == ["ts1"]


def test_a_permanent_refusal_is_recorded_as_not_sent() -> None:
    channel = _Refusing("permanent", 1)
    begins, results = _run(channel)
    assert channel.sent == []
    assert begins == 1
    assert results == ["not sent: permanent"]
