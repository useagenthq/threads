"""recovered() includes a follow-on recovery owns even when it was deferred: the follow-on found a
later run live on its branch, so it goes on once that run ends, and the seam waits for that
(Codex #372 MEDIUM)."""

import asyncio
from collections.abc import Sequence

import pytest
from host.test_channel_recovery import (
    Replies,
    _consumed,  # pyright: ignore[reportPrivateUsage] - the shared probe
    text,
    until,
    webhook,
)
from host.test_host_stop import (
    _root,  # pyright: ignore[reportPrivateUsage] - the shared lookup
)

from threads import agent, extension, scripted_model, sqlite
from threads.agents.context import RunContext
from threads.host import host
from threads.host.app import recovered
from threads.thread import tree

STOP_S = 2.0


def test_recovered_waits_for_a_follow_on_deferred_behind_a_later_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = sqlite(":memory:")

    async def main() -> None:
        later, entered, release_later = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def gated(_source: object, _ctx: RunContext[None]) -> Sequence[str]:
            if later.is_set() and not release_later.is_set():
                entered.set()
                await release_later.wait()
            return ()

        bot = agent(
            model=scripted_model({"responses": [text("Hi there."), text("later")]}),
            extensions=[extension(name="gated", hooks={"session_start": gated})],
        )
        # A host that died right after its turn ended: recovery owes the reply.
        async with host(
            store=store, agents={"bot": bot}, channels={"fake": Replies(crash=True)}
        ) as first:
            await first.receive("fake", webhook("d1", "m1", "hello"))
            await until(lambda: _consumed(store))
        hold = asyncio.Event()
        channel = Replies(hold=hold)
        served = host(store=store, agents={"bot": bot}, channels={"fake": channel})
        runner = served._runner  # pyright: ignore[reportPrivateUsage] - to answer a control
        await served.ready()
        await channel.sending.wait()
        tenant, thread_id, branch = await _root(runner)
        # A control while recovery's run is mid-send: its follow-on is queued behind it.
        await runner.resume(tenant, thread_id, branch)
        # The follow-on stops in its root lookup while a later message takes the branch.
        in_lookup, go_on = asyncio.Event(), asyncio.Event()
        root_of = tree.root_of

        async def held(*args: object, **kwargs: object) -> object:
            if not kwargs and not go_on.is_set():
                in_lookup.set()
                await go_on.wait()
            return await root_of(*args, **kwargs)  # pyright: ignore[reportArgumentType] - a spy

        monkeypatch.setattr(tree, "root_of", held)
        hold.set()
        await in_lookup.wait()
        later.set()
        await served.receive("fake", webhook("d2", "m2", "again"))
        await asyncio.wait_for(entered.wait(), STOP_S)
        go_on.set()
        seam = asyncio.ensure_future(recovered(served))
        # The follow-on waits for the later run to end, and so does the seam.
        done, _ = await asyncio.wait({seam}, timeout=0.2)
        assert not done
        release_later.set()
        await asyncio.wait_for(seam, STOP_S)
        await asyncio.wait_for(served.stop(), STOP_S)

    asyncio.run(main())
