"""stop() aborts first: runs are cancelled before intake in flight is waited on, so a consumer
waiting on its run never holds shutdown up, and a send that never answers stays begun (in doubt,
for the next start to reconcile), never assumed unsent. stop() also ends follow-on resumes, so a
restart runs nothing of the old generation. The recovery seam waits on the runs its pass started
and their follow-ons, never on unrelated live runs."""

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
from threads.host.runs import Bound, Runner
from threads.log import BranchId, EffectBeginEvent, Event, JsonObject, ThreadId
from threads.memory.fence import check
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


async def _root(runner: Runner) -> tuple[Store, ThreadId, BranchId]:
    tenant = runner.store(TEAM)
    sq = await open_store(tenant)
    thread_id = (await sq.tables.inbox_rows())[0].thread_id
    root = await sq.root(thread_id)
    assert isinstance(root, Ok)
    return tenant, thread_id, root.value


def test_stop_cancels_a_follow_on_resume_so_a_restart_runs_nothing_old() -> None:
    store = sqlite(":memory:")

    async def main() -> None:
        await crashed(store)
        hold = asyncio.Event()
        bot = agent(model=scripted_model({"responses": []}))
        runner = Runner(store, {"bot": bot}, {"fake": Replies(hold=hold)})
        runner.resolve_secrets()
        tenant, thread_id, branch = await _root(runner)
        await runner.redeliver(tenant, thread_id)
        await until(lambda: _running(runner, branch))
        # A control while the recovered run is in flight: its resume follows once that run ends.
        await runner.resume(tenant, thread_id, branch)
        # The follow-on stalls in its store reads until the barrier opens.
        barrier, reached = asyncio.Event(), asyncio.Event()
        bound = runner.bound

        async def stalled(store: Store, thread: ThreadId) -> Bound | None:
            reached.set()
            await barrier.wait()
            return await bound(store, thread)

        runner.bound = stalled  # pyright: ignore[reportAttributeAccessIssue] - the barrier
        follow_ons = runner._pending  # pyright: ignore[reportPrivateUsage] - what stop owns
        hold.set()
        await reached.wait()
        await runner.stop()
        assert all(t.done() for t in follow_ons)
        # Started again at once: the old generation's resume launches nothing.
        runner.open()
        barrier.set()
        for _ in range(20):
            await asyncio.sleep(0)
        assert not runner.running(branch)

    asyncio.run(main())


def test_recovered_waits_for_the_follow_on_its_own_recovery_owns() -> None:
    store = sqlite(":memory:")

    async def main() -> None:
        await crashed(store)
        channel = Replies(hold=asyncio.Event())
        bot = agent(model=scripted_model({"responses": []}))
        served = host(store=store, agents={"bot": bot}, channels={"fake": channel})
        runner = served._runner  # pyright: ignore[reportPrivateUsage] - to queue a follow-on
        await served.ready()
        await channel.sending.wait()
        tenant, thread_id, branch = await _root(runner)
        # A control while recovery's run is mid-send: its resume follows once that run ends.
        await runner.resume(tenant, thread_id, branch)
        assert channel.hold is not None
        channel.hold.set()
        await asyncio.wait_for(recovered(served), STOP_S)
        follow_ons = runner._pending  # pyright: ignore[reportPrivateUsage] - what recovery owns
        assert follow_ons
        assert all(t.done() for t in follow_ons)
        assert not runner.running(branch)
        await served.stop()

    asyncio.run(main())


async def _running(runner: Runner, branch: BranchId) -> bool:
    return runner.running(branch)


class _PastTheFence(Replies):
    """A send whose request passes the transport fence, then waits on `hold` before it lands."""

    async def perform(
        self, op: JsonObject, effect_key: str, credentials: Mapping[str, str]
    ) -> DeliveryOutcome:
        await check()
        return await Replies.perform(self, op, effect_key, credentials)


def test_a_send_past_the_fence_keeps_its_lease_until_it_settles() -> None:
    store = sqlite(":memory:")

    async def main() -> None:
        hold = asyncio.Event()
        channel = _PastTheFence(hold=hold)
        bot = agent(model=scripted_model({"responses": [text("hi")]}))
        served = host(store=store, agents={"bot": bot}, channels={"fake": channel})
        await served.ready()
        await served.receive("fake", webhook("d1", "m1", "hello"))
        await channel.sending.wait()
        stopping = asyncio.ensure_future(served.stop())
        # Past the fence the request may still land: stop() waits, and the lease stays held.
        done, _ = await asyncio.wait({stopping}, timeout=0.2)
        assert not done
        hold.set()
        await asyncio.wait_for(stopping, STOP_S)
        # It landed before the lease was released. The cancelled run records no outcome, so the
        # effect stays begun, and the next start's lookup finds the send instead of repeating it.
        assert [op["text"] for op in channel.sent] == ["hi"]
        types = [e.type for e in await _events(store)]
        assert "effect_begin" in types
        assert "effect_resolved" not in types

    asyncio.run(main())
