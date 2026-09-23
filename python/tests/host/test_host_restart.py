"""A request a host took before stop() never starts a run after it: stop() ends that
generation, and a host started again runs only what it is asked from then on. Found by Codex
review #332: a start_run() or a control's resume() held before its run was registered survived
stop() and ready() and launched afterwards."""

import asyncio
from collections.abc import Sequence

import pytest
from host.test_channel_recovery import text

from threads import agent, extension, scripted_model, sqlite
from threads._generated.host_api_v1 import StartRunRequest
from threads.agents.context import RunContext
from threads.agents.store import Store, open_store
from threads.host import host
from threads.host import start as host_start
from threads.host.runs import Bound, Runner
from threads.log import BranchId, Principal, ThreadId, UserInputEvent
from threads.result import Err, Ok
from threads.thread import tree
from threads.thread.handle import Thread

ALICE = Principal(issuer="api", tenant="acme", subject="alice")


def test_a_start_run_held_across_stop_and_restart_starts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        served = host(
            store=store, agents={"bot": agent(model=scripted_model({"responses": [text("ok")]}))}
        )
        await served.ready()
        entered, release = asyncio.Event(), asyncio.Event()
        target = host_start._target  # pyright: ignore[reportPrivateUsage] - the barrier

        async def held(*args: object) -> object:
            entered.set()
            await release.wait()
            return await target(*args)  # pyright: ignore[reportArgumentType] - a spy

        monkeypatch.setattr(host_start, "_target", held)
        request = StartRunRequest.model_validate({"agent": "bot", "input": "hello"})
        pending = asyncio.ensure_future(
            served.start_run(request, principal=ALICE, idempotency_key="k")
        )
        await entered.wait()
        await served.stop()
        await served.ready()
        release.set()
        answer = await pending
        # Retryable, as for a branch another run holds: the same key starts it on this host.
        assert isinstance(answer, Err)
        assert answer.error.code == "branch_busy"
        assert not await _inputs(served.thread, store)
        await served.stop()

    asyncio.run(main())


def test_a_resume_held_across_stop_and_restart_starts_nothing() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        served = host(
            store=store,
            agents={"bot": agent(model=scripted_model({"responses": [text("ok"), text("again")]}))},
        )
        await served.ready()
        request = StartRunRequest.model_validate({"agent": "bot", "input": "hello"})
        accepted = await served.start_run(request, principal=ALICE, idempotency_key="k")
        assert isinstance(accepted, Ok)
        runner = served._runner  # pyright: ignore[reportPrivateUsage] - the barrier
        thread_id = accepted.value.thread_id
        branch = accepted.value.branch_id
        while runner.running(branch):
            await asyncio.sleep(0.01)
        entered, release = asyncio.Event(), asyncio.Event()
        bound = runner.bound

        async def held(store: Store, thread: ThreadId) -> Bound | None:
            entered.set()
            await release.wait()
            return await bound(store, thread)

        runner.bound = held  # pyright: ignore[reportAttributeAccessIssue] - the barrier
        tenant = runner.store(ALICE.tenant)
        pending = asyncio.ensure_future(served.resume(Thread(thread_id, branch, tenant)))
        await entered.wait()
        await served.stop()
        await served.ready()
        release.set()
        await pending
        assert not runner.running(branch)
        await served.stop()

    asyncio.run(main())


def test_a_refused_old_request_leaves_the_live_run_on_its_branch_tracked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex #344 HIGH 1: a refused request from before a stop must not replace the record of the
    run now going on its branch, or stop() would miss that run."""

    async def main() -> None:
        hold, entered = asyncio.Event(), asyncio.Event()

        async def gated(_source: object, _ctx: RunContext[None]) -> Sequence[str]:
            if hold.is_set():
                entered.set()
                await asyncio.Event().wait()
            return ()

        replies = [text("one"), text("two"), text("three")]
        bot = agent(
            model=scripted_model({"responses": replies}),
            extensions=[extension(name="gated", hooks={"session_start": gated})],
        )
        served = host(store=sqlite(":memory:"), agents={"bot": bot})
        await served.ready()
        first = await served.start_run(_ask("a"), principal=ALICE, idempotency_key="k1")
        assert isinstance(first, Ok)
        thread, branch = first.value.thread_id, first.value.branch_id
        runner = served._runner  # pyright: ignore[reportPrivateUsage] - what stop() tracks
        while runner.running(branch):
            await asyncio.sleep(0.01)
        # An old request held before its run registers, then a stop and a restart.
        entered_old, release = asyncio.Event(), asyncio.Event()
        target = host_start._target  # pyright: ignore[reportPrivateUsage] - the barrier

        async def held(*args: object) -> object:
            entered_old.set()
            await release.wait()
            return await target(*args)  # pyright: ignore[reportArgumentType] - a spy

        monkeypatch.setattr(host_start, "_target", held)
        old = asyncio.ensure_future(
            served.start_run(_ask("b", thread), principal=ALICE, idempotency_key="k2")
        )
        await entered_old.wait()
        await served.stop()
        await served.ready()
        monkeypatch.setattr(host_start, "_target", target)
        # A fresh run on the same branch, held in session_start.
        hold.set()
        current = asyncio.ensure_future(
            served.start_run(_ask("c", thread), principal=ALICE, idempotency_key="k3")
        )
        await entered.wait()
        release.set()
        refused = await old
        assert isinstance(refused, Err)
        assert refused.error.code == "branch_busy"
        # The live run is still the branch's run, so stop() ends it.
        assert runner.running(branch)
        await asyncio.wait_for(served.stop(), 2.0)
        assert not runner.running(branch)
        # Ended by the stop before it recorded its input: refused, retryable.
        stopped = await asyncio.wait_for(current, 2.0)
        assert isinstance(stopped, Err)

    asyncio.run(main())


def test_stop_drains_every_follow_on_of_a_branch_and_none_runs_after_restart() -> None:
    """Codex #344 HIGH 2: a second follow-on on a branch must not hide the first from stop()."""

    async def main() -> None:
        runner = Runner(sqlite(":memory:"), {}, {})
        branch = BranchId("01a0cf30-3d8e-7bb8-b294-1dcc47b62a40")
        thread_id = ThreadId("01a0cf30-3969-7caf-adb4-abbec478caa6")
        thread = Thread(thread_id, branch, runner.store("acme"))
        gate, launched = asyncio.Event(), list[int]()

        async def held(*_args: object, **_kwargs: object) -> None:
            await gate.wait()
            launched.append(1)

        runner.resume = held  # the barrier

        async def ended() -> None:
            return None

        for _ in range(2):
            run = asyncio.ensure_future(ended())
            await run
            runner._again.add(branch)  # pyright: ignore[reportPrivateUsage] - a queued control
            runner._ended(thread, run)  # pyright: ignore[reportPrivateUsage, reportArgumentType] - a run that ended
        await asyncio.sleep(0)
        await runner.stop()
        runner.open()
        gate.set()
        for _ in range(20):
            await asyncio.sleep(0)
        assert launched == []

    asyncio.run(main())


def test_a_resume_from_before_stop_queues_nothing_behind_a_fresh_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex #372 HIGH: a Host.resume() from before a stop that finds a fresh run on its branch
    after the restart must not queue a follow-on behind it: that would start a third run."""

    async def main() -> None:
        started, hold, release_fresh = [0], asyncio.Event(), asyncio.Event()
        entered = asyncio.Event()

        async def counted(_source: object, _ctx: RunContext[None]) -> Sequence[str]:
            started[0] += 1
            if hold.is_set() and not release_fresh.is_set():
                entered.set()
                await release_fresh.wait()
            return ()

        replies = [text("one"), text("two"), text("three")]
        bot = agent(
            model=scripted_model({"responses": replies}),
            extensions=[extension(name="counted", hooks={"session_start": counted})],
        )
        served = host(store=sqlite(":memory:"), agents={"bot": bot})
        await served.ready()
        first = await served.start_run(_ask("a"), principal=ALICE, idempotency_key="k1")
        assert isinstance(first, Ok)
        thread, branch = first.value.thread_id, first.value.branch_id
        runner = served._runner  # pyright: ignore[reportPrivateUsage] - what runs here
        while runner.running(branch):
            await asyncio.sleep(0.01)
        # The old resume, held in its root lookup across a stop and a restart.
        in_lookup, release = asyncio.Event(), asyncio.Event()
        root_of = tree.root_of

        async def held(*args: object, **kwargs: object) -> object:
            if not kwargs and not release.is_set():
                in_lookup.set()
                await release.wait()
            return await root_of(*args, **kwargs)  # pyright: ignore[reportArgumentType] - a spy

        monkeypatch.setattr(tree, "root_of", held)
        tenant = runner.store(ALICE.tenant)
        old = asyncio.ensure_future(served.resume(Thread(thread, branch, tenant)))
        await in_lookup.wait()
        await served.stop()
        await served.ready()
        # A fresh run on the branch, held in session_start, when the old resume goes on.
        hold.set()
        fresh = asyncio.ensure_future(
            served.start_run(_ask("c", thread), principal=ALICE, idempotency_key="k3")
        )
        await entered.wait()
        release.set()
        await old
        release_fresh.set()
        assert isinstance(await fresh, Ok)
        await runner.settled()
        # The first run and the fresh one: the old resume started nothing, now or queued.
        assert started[0] == len(("first", "fresh"))
        await served.stop()

    asyncio.run(main())


def _ask(words: str, thread: ThreadId | None = None) -> StartRunRequest:
    body: dict[str, object] = {"agent": "bot", "input": words}
    if thread is not None:
        body["thread_id"] = thread
    return StartRunRequest.model_validate(body)


async def _inputs(_thread: object, store: Store) -> list[UserInputEvent]:
    sq = await open_store(store)
    rows = await sq.run(lambda c: c.execute("SELECT thread_id FROM branches").fetchall())
    found: list[UserInputEvent] = []
    for (thread_id,) in rows or []:
        root = await sq.root(ThreadId(thread_id))
        read = None if not isinstance(root, Ok) else await sq.read(root.value, 0)
        if isinstance(read, Ok):
            found += [e for e in read.value.fold.events if isinstance(e, UserInputEvent)]
    return found
