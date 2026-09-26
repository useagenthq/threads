"""Thread.cancel across processes (lane 29F). Another process's lease is no longer branch_busy:
the cancel becomes a durable `api` control item, and the branch's holder applies it at its next
step boundary, exactly once, before anything it would dispatch next."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from cancel_kit import said
from control_item_kit import expire_leases, resumed, rows
from corpus import Clock
from kit import Tools, kinds, open_store, start, text
from team.crash_kit import OPERATOR

from threads._generated.host_api_v1 import Appended, CancelAccepted
from threads.agents.store import now_ms, sqlite
from threads.log import Principal, ThreadId
from threads.loop.drive import drive
from threads.loop.model import ModelChunk, ModelContext, ModelRequest
from threads.loop.runtime import Runtime
from threads.loop.scripted import ScriptedModel, scripted_model
from threads.result import Err, Ok
from threads.store import SqliteStore
from threads.thread.control import Accepted
from threads.thread.control_items import API_CHANNEL
from threads.thread.handle import Thread

STRANGER = Principal(issuer="api", tenant="other", subject="mallory")


def _db(where: Path) -> Path:
    return where / "threads.db"


def _clock() -> Clock:
    """Real time: the requesting process reads the holder's lease against its own wall clock, so
    a fixture clock in the past would make that lease look expired."""
    return Clock(now_ms())


def _never() -> ScriptedModel:
    return scripted_model({"responses": [text("never"), text("never")]})


async def _holder(where: Path, model: ScriptedModel, clock: Clock) -> tuple[SqliteStore, Runtime]:
    """A run holding the branch's lease, as another process's would: its writer is not this
    process's live one, so a cancel here can't append through it."""
    sq = await open_store(_db(where))
    return sq, await start(sq, [], model, Tools({}, clock), clock)


def _thread(where: Path, rt: Runtime) -> Thread:
    """The requesting process's handle on the same thread, over its own connection."""
    thread = rt.fold.thread_id
    assert thread is not None
    return Thread(ThreadId(thread), rt.writer.branch_id, sqlite(str(where)))


def accepted(done: Accepted) -> CancelAccepted:
    """The durable item Thread.cancel returned; anything else fails the drill."""
    assert isinstance(done, Ok), done
    item = done.value
    assert isinstance(item, CancelAccepted), item
    return item


def after_barrier(log: list[str]) -> list[str]:
    """The event types after the barrier; a dispatch among them breaks invariant 2."""
    return log[log.index("cancel_requested") + 1 :]


def test_a_lease_held_elsewhere_writes_one_durable_item(tmp_path: Path) -> None:
    async def main() -> None:
        clock = _clock()
        sq, rt = await _holder(tmp_path, _never(), clock)
        try:
            accepted(await _thread(tmp_path, rt).cancel(OPERATOR))
            item = rows(_db(tmp_path))
            assert len(item) == 1
            assert item[0].channel == API_CHANNEL
            assert item[0].consumed_seq is None
            # The request appends nothing: the log records the cancel where it is applied.
            assert "cancel_requested" not in kinds(rt.events)
        finally:
            await sq.close()

    asyncio.run(main())


def test_a_second_ask_queues_no_second_item(tmp_path: Path) -> None:
    """A parent barring a busy child at every step must not grow the queue."""

    async def main() -> None:
        clock = _clock()
        sq, rt = await _holder(tmp_path, _never(), clock)
        try:
            thread = _thread(tmp_path, rt)
            first = accepted(await thread.cancel(OPERATOR))
            again = accepted(await thread.cancel(OPERATOR))
            assert first.item_key == again.item_key
            assert len(rows(_db(tmp_path))) == 1
        finally:
            await sq.close()

    asyncio.run(main())


def test_another_tenants_principal_is_forbidden_and_writes_nothing(tmp_path: Path) -> None:
    async def main() -> None:
        clock = _clock()
        sq, rt = await _holder(tmp_path, _never(), clock)
        try:
            done = await _thread(tmp_path, rt).cancel(STRANGER)
            assert isinstance(done, Err)
            assert done.error.code == "forbidden"
            assert rows(_db(tmp_path)) == ()
        finally:
            await sq.close()

    asyncio.run(main())


def test_a_free_branch_still_appends_its_barrier_with_no_item(tmp_path: Path) -> None:
    async def main() -> None:
        clock = _clock()
        sq, rt = await _holder(tmp_path, _never(), clock)
        thread = _thread(tmp_path, rt)
        try:
            await rt.writer.release()
            done = await thread.cancel(OPERATOR)
            assert isinstance(done, Ok), done
            assert isinstance(done.value, Appended)
            assert rows(_db(tmp_path)) == ()
        finally:
            await sq.close()

    asyncio.run(main())


def test_the_next_lease_taker_applies_it_first_before_any_dispatch(tmp_path: Path) -> None:
    """The holder died with the turn open and the item pending: whoever takes the lease applies
    it before it dispatches anything of its own."""

    async def main() -> tuple[list[str], ScriptedModel]:
        clock = _clock()
        model = _never()
        sq, rt = await _holder(tmp_path, model, clock)
        branch = rt.writer.branch_id
        try:
            accepted(await _thread(tmp_path, rt).cancel(OPERATOR))
            expire_leases(_db(tmp_path))
            next_run = await resumed(sq, branch, model, clock)
            await drive(next_run)
            return kinds(next_run.events), model
        finally:
            await sq.close()

    log, model = asyncio.run(main())
    assert "model_request" not in log
    assert log.count("cancel_requested") == 1
    assert "cancelled" in log
    assert model.sent == []
    item = rows(_db(tmp_path))
    assert len(item) == 1
    assert item[0].consumed_seq is not None


class _ItemAtSend(ScriptedModel):
    """Host B writes the item while this send is in flight, as another process would."""

    thread: Thread

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        accepted(await self.thread.cancel(OPERATOR))
        async for chunk in super().send(request, context):
            yield chunk


def test_an_item_during_an_in_flight_call_stops_the_next_dispatch(tmp_path: Path) -> None:
    """A cancel item from another host against a holder's in-flight model call: no dispatch after
    the barrier's commit, and the item is applied once."""

    async def main() -> list[str]:
        clock = _clock()
        model = _ItemAtSend([said("one"), said("two")], {})
        sq, rt = await _holder(tmp_path, model, clock)
        try:
            model.thread = _thread(tmp_path, rt)
            await drive(rt)
            return kinds(rt.events)
        finally:
            await sq.close()

    log = asyncio.run(main())
    assert log.count("model_request") == 1
    assert log.count("cancel_requested") == 1
    assert "model_request" not in after_barrier(log)
    assert "cancelled" in log
    item = rows(_db(tmp_path))
    assert len(item) == 1
    assert item[0].consumed_seq is not None
