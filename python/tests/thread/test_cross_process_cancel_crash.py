"""The crash drills of the cross-process cancel (lane 29F): a kill right after the item's insert
leaves the holder one item and it applies it once, and a holder killed right after applying
dispatches nothing on the restart."""

import asyncio
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest
from control_item_kit import expire_leases, resumed, rows
from corpus import Clock
from kit import Tools, kinds, open_store, start, text
from team.crash_kit import OPERATOR, CrashError, Point, crashing, reached
from test_cross_process_cancel import accepted, after_barrier

from threads.agents.store import Store, now_ms, sqlite
from threads.agents.store import open_store as store_handle
from threads.log import ThreadId
from threads.loop.drive import drive
from threads.loop.runtime import Runtime
from threads.loop.scripted import ScriptedModel, scripted_model
from threads.thread.handle import Thread

_BEFORE, _WRITTEN, _COMMITTED = 0, 1, 2
"""A kill point's stages: before the row is written, written, and its transaction committed."""


def _never() -> ScriptedModel:
    return scripted_model({"responses": [text("never"), text("never")]})


def _clock() -> Clock:
    """Real time: a lease is read against the wall clock of whoever reads it."""
    return Clock(now_ms())


def _after_the_item() -> Point:
    """Dies on the first statement after the item's insert has committed: the requester's process
    is gone, and the row it wrote is durable."""
    stage = [_BEFORE]

    def at(_conn: sqlite3.Connection, sql: str, _params: Sequence[object]) -> bool:
        if stage[0] == _COMMITTED:
            return True
        if stage[0] == _WRITTEN and sql.startswith("COMMIT"):
            stage[0] = _COMMITTED
        elif stage[0] == _BEFORE and "INSERT INTO inbox" in sql:
            stage[0] = _WRITTEN
        return False

    return Point("the statement after the item's insert", at)


def _at_the_turns_end() -> Point:
    """Dies inside the append after the one that applied the item, so the barrier and the item's
    consumption are durable and the turn's own end is not: the item is consumed, then that
    transaction commits, then the next append moves the head. Event rows go in with `executemany`,
    which this connection doesn't see, so the head checkpoint is the point."""
    stage = [_BEFORE]

    def at(_conn: sqlite3.Connection, sql: str, _params: Sequence[object]) -> bool:
        if stage[0] == _COMMITTED:
            return "UPDATE branches SET head_seq" in sql
        if stage[0] == _WRITTEN and sql.startswith("COMMIT"):
            stage[0] = _COMMITTED
        elif stage[0] == _BEFORE and "UPDATE inbox SET consumed_seq" in sql:
            stage[0] = _WRITTEN
        return False

    return Point("the cancelled turn's end", at)


def _thread(rt: Runtime, store: Store) -> Thread:
    thread = rt.fold.thread_id
    assert thread is not None
    return Thread(ThreadId(thread), rt.writer.branch_id, store)


def test_a_kill_after_the_items_insert_leaves_one_item_applied_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    where = tmp_path / "s"
    db = where / "threads.db"

    async def main() -> tuple[list[str], ScriptedModel]:
        clock = _clock()
        model = _never()
        dying = await crashing(where, monkeypatch, _after_the_item())
        sq = await open_store(db)
        try:
            rt = await start(sq, [], model, Tools({}, clock), clock)
            with pytest.raises(CrashError):
                await _thread(rt, dying).cancel(OPERATOR)
            assert reached()
            pending = rows(db)
            assert len(pending) == 1
            assert pending[0].consumed_seq is None
            # The holder applies it at its next boundary, once.
            await drive(rt)
            return kinds(rt.events), model
        finally:
            await sq.close()

    log, model = asyncio.run(main())
    assert log.count("cancel_requested") == 1
    assert "model_request" not in log
    assert model.sent == []
    applied = rows(db)
    assert len(applied) == 1
    assert applied[0].consumed_seq is not None


def test_a_holder_killed_right_after_applying_dispatches_nothing_on_the_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    where = tmp_path / "s"
    db = where / "threads.db"

    async def main() -> tuple[list[str], ScriptedModel]:
        clock = _clock()
        model = _never()
        dying = await crashing(where, monkeypatch, _at_the_turns_end())
        sq = await store_handle(dying)
        rt = await start(sq, [], model, Tools({}, clock), clock)
        branch = rt.writer.branch_id
        accepted(await _thread(rt, sqlite(str(where))).cancel(OPERATOR))
        with pytest.raises(CrashError):
            await drive(rt)
        assert reached()
        # The barrier and the item's consumption were one transaction, and they committed.
        applied = rows(db)
        assert len(applied) == 1
        assert applied[0].consumed_seq is not None
        assert "cancel_requested" in kinds(rt.events)
        expire_leases(db)
        fresh = await open_store(db)
        try:
            restarted = await resumed(fresh, branch, model, clock)
            await drive(restarted)
            return kinds(restarted.events), model
        finally:
            await fresh.close()

    log, model = asyncio.run(main())
    assert log.count("cancel_requested") == 1
    assert "model_request" not in after_barrier(log)
    assert "cancelled" in log
    assert model.sent == []
