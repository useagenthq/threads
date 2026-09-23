"""A request a host took before stop() never starts a run after it: stop() ends that
generation, and a host started again runs only what it is asked from then on. Found by Codex
review #332: a start_run() or a control's resume() held before its run was registered survived
stop() and ready() and launched afterwards."""

import asyncio

import pytest
from host.test_channel_recovery import text

from threads import agent, scripted_model, sqlite
from threads._generated.host_api_v1 import StartRunRequest
from threads.agents.store import Store, open_store
from threads.host import host
from threads.host import start as host_start
from threads.host.runs import Bound
from threads.log import Principal, ThreadId, UserInputEvent
from threads.result import Err, Ok
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
