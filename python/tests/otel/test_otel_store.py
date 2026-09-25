"""The exporter over a real store: forks, every tick position, bad and stuck branches, other
writers, and what a deletion records (spec/otel/README.md, "The cursor, sync and losses")."""

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from otel_collector_kit import Collector, collector
from otel_store_kit import CASES, cursors, imported, losses, query

from threads import sqlite
from threads._generated.store_sql import STORE_VERSION
from threads.agents.store import Store, open_store
from threads.log import BranchId, ThreadId
from threads.log.digest import sha256_hex
from threads.otel import otel
from threads.otel.ids import span_id
from threads.result import Ok
from threads.store import SqliteStore
from threads.store._feed import Checkpoint, Feed
from threads.store.conn import Conn, one
from threads.store.deletion import delete_thread
from threads.store.sql import blob_of
from threads.telemetry import SyncReport

GOLDENS = sorted(p.name for p in CASES.iterdir())
PARENT = "0192b000-0000-7000-8000-000000000001"
FORK = "0192b000-0000-7000-8000-000000000003"
CHILD = "0192b000-0000-7000-8000-000000000002"
DELETED_AT = 1_790_000_100_000


async def _sync(store: Store, c: Collector, name: str | None = None) -> SyncReport:
    sent = await otel(store=store, endpoint=c.url, name=name).sync()
    assert isinstance(sent, Ok), sent
    return sent.value


def _spans(c: Collector) -> list[str]:
    """Each accepted span as canonical-ish JSON, for comparing multisets."""
    return sorted(json.dumps(s, sort_keys=True) for r in c.accepted() for s in r.spans())


def test_a_fork_exports_only_its_own_segment(tmp_path: Path) -> None:
    async def main() -> None:
        async with collector() as c:
            store = sqlite(str(tmp_path / "s"))
            await imported(store, "otel-fork-no-reexport", only=(PARENT,))
            first = await _sync(store, c)
            parent_ids = set(c.span_ids())
            assert first.spans == len(parent_ids) == 6  # noqa: PLR2004 - counted spans
            await imported(store, "otel-fork-no-reexport", only=(FORK,))
            await _sync(store, c)
            fork_ids = c.span_ids()[len(parent_ids) :]
            assert len(fork_ids) == 2  # noqa: PLR2004 - counted spans
            assert not parent_ids & set(fork_ids)
            fork_log = (CASES / "otel-fork-no-reexport" / f"{FORK}.jsonl").read_bytes()
            opener = next(
                json.loads(ln)
                for ln in fork_log.splitlines()
                if b'"user_input"' in ln and FORK.encode() in ln
            )
            assert span_id(FORK, opener["event_id"]) in fork_ids

    asyncio.run(main())


async def _rows(store: Store) -> list[tuple[object, ...]]:
    return await query(
        store,
        "SELECT e.branch_id, seq, event_id, type, type_version, critical, epoch, line"
        " FROM events e JOIN branches b ON b.branch_id = e.branch_id ORDER BY b.rowid, seq",
    )


async def _empty(store: Store) -> None:
    """Every branch back to no rows of its own (a fork at its fork point)."""

    def cut(conn: Conn) -> None:
        conn.execute("DELETE FROM events")
        conn.execute(
            "UPDATE branches SET head_seq = coalesce(fork_at_seq, 0),"
            " head_hash = lower(hex(randomblob(32)))"
        )

    await (await open_store(store)).run(cut)


async def _cut(store: Store, branch: str, seq: int) -> None:
    """A root branch's rows after `seq` removed, its head at `seq`."""

    def cut(conn: Conn) -> None:
        conn.execute("DELETE FROM events WHERE branch_id = ? AND seq > ?", (branch, seq))
        found = conn.execute(
            "SELECT line FROM events WHERE branch_id = ? AND seq = ?", (branch, seq)
        ).fetchone()
        header = conn.execute(
            "SELECT header_line FROM branches WHERE branch_id = ?", (branch,)
        ).fetchone()
        last = blob_of((found if found is not None else one(header))[0])
        conn.execute(
            "UPDATE branches SET head_seq = ?, head_hash = ? WHERE branch_id = ?",
            (seq, sha256_hex(last), branch),
        )

    await (await open_store(store)).run(cut)


async def _grow(store: Store, row: tuple[object, ...]) -> None:
    def put(conn: Conn) -> None:
        conn.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)
        line = row[7]
        assert isinstance(line, bytes)
        conn.execute(
            "UPDATE branches SET head_seq = ?, head_hash = ? WHERE branch_id = ?",
            (row[1], sha256_hex(line), row[0]),
        )

    await (await open_store(store)).run(put)


@pytest.mark.parametrize("case", GOLDENS)
def test_a_sync_after_every_append_sends_what_one_sync_sends(tmp_path: Path, case: str) -> None:
    async def main() -> None:
        async with collector() as once, collector() as ticked:
            whole = sqlite(str(tmp_path / "whole"))
            await imported(whole, case)
            await _sync(whole, once)
            grown = sqlite(str(tmp_path / "grown"))
            await imported(grown, case)
            rows = await _rows(grown)
            await _empty(grown)
            for row in rows:
                await _grow(grown, row)
                await _sync(grown, ticked)
            assert _spans(ticked) == _spans(once)
            assert await cursors(grown) == await cursors(whole)

    asyncio.run(main())


async def _delete(store: Store, thread: str) -> None:
    sq = await open_store(store)
    deleted = await sq.run(lambda c: delete_thread(c, "local", ThreadId(thread), DELETED_AT))
    assert isinstance(deleted, Ok)


def test_a_delete_counts_what_the_observer_had_not_sent(tmp_path: Path) -> None:
    """At every tick position: the loss row counts the events past the cursor, is sent once as
    a possibly_lost span with the same ids, and other threads are untouched."""
    case = "otel-subagent-same-trace"
    child_thread = "0192a000-0000-7000-8000-0000000000c1"

    async def at(tick: int) -> str:
        """Synced with the child's log through `tick`, then the rest appended, then deleted."""
        async with collector() as c:
            store = sqlite(str(tmp_path / f"s{tick}"))
            await imported(store, case)
            rows = [r for r in await _rows(store) if r[0] == CHILD]
            await _cut(store, CHILD, tick)
            await _sync(store, c)
            for row in rows[tick:]:
                await _grow(store, row)
            parent_cursor = (await cursors(store))[PARENT]
            await _delete(store, child_thread)
            report = await _sync(store, c)
            assert report.possibly_lost_events == len(rows) - tick
            lost = c.accepted()[-1].spans()
            assert [s["name"] for s in lost] == ["threads.export.possibly_lost"]
            assert await losses(store) == [(child_thread, len(rows) - tick, True)]
            assert (await _sync(store, c)).possibly_lost_events == 0  # sent once
            assert (await cursors(store))[PARENT] == parent_cursor
            return f"{lost[0]['traceId']}/{lost[0]['spanId']}"

    async def main() -> None:
        ids = {await at(tick) for tick in range(6)}
        assert len(ids) == 1  # the same span at every tick position

    asyncio.run(main())


def test_no_registered_observer_records_no_loss(tmp_path: Path) -> None:
    async def main() -> None:
        store = sqlite(str(tmp_path / "s"))
        await imported(store, "otel-subagent-same-trace")
        await _delete(store, "0192a000-0000-7000-8000-0000000000c1")
        assert await losses(store) == []

    asyncio.run(main())


def test_older_stores_are_refused(tmp_path: Path) -> None:
    async def main() -> None:
        for version in range(1, STORE_VERSION):
            path = tmp_path / f"v{version}.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE t (x)")
            conn.execute(f"PRAGMA user_version = {version}")
            conn.close()
            opened = await SqliteStore.open(path)
            assert not isinstance(opened, Ok)
            assert opened.error.code == "unsupported_format"
            assert "create a new store" in opened.error.message

    asyncio.run(main())


def test_the_feed_never_appends_and_never_moves_a_cursor_back(tmp_path: Path) -> None:
    async def main() -> None:
        async with collector() as c:
            store = sqlite(str(tmp_path / "s"))
            await imported(store, "otel-turn-model-tool")
            heads = await query(store, "SELECT branch_id, head_seq, head_hash FROM branches")
            await _sync(store, c)
            assert (
                await query(store, "SELECT branch_id, head_seq, head_hash FROM branches") == heads
            )
            feed: Feed = (await open_store(store)).feed("otel", lambda: 0)
            await feed.checkpoint([Checkpoint(BranchId(PARENT), 3)])
            assert await cursors(store) == {PARENT: 10}

    asyncio.run(main())


def test_a_branch_another_handle_writes_is_exported(tmp_path: Path) -> None:
    async def main() -> None:
        async with collector() as c:
            store = sqlite(str(tmp_path / "s"))
            assert (await _sync(store, c)).spans == 0
            other = sqlite(str(tmp_path / "s"))
            assert other is not store
            await imported(other, "otel-turn-model-tool")
            assert (await _sync(store, c)).spans == 4  # noqa: PLR2004 - counted spans

    asyncio.run(main())


def _spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every branch the feed reads, in order."""
    reads: list[str] = []
    real = Feed.chain

    async def spy(self: Feed, branch_id: BranchId) -> object:
        reads.append(branch_id)
        return await real(self, branch_id)

    monkeypatch.setattr(Feed, "chain", spy)
    return reads


def test_a_corrupt_branch_is_skipped_and_backed_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = _spy(monkeypatch)

    async def main() -> None:
        async with collector() as c:
            store = sqlite(str(tmp_path / "s"))
            await imported(store, "otel-subagent-same-trace")
            await query(
                store,
                "UPDATE events SET line = CAST(replace(CAST(line AS TEXT), 'review', 'rewrite')"
                " AS BLOB) WHERE branch_id = ? AND seq = 1",
                CHILD,
            )
            first = await _sync(store, c)
            assert first.spans == 4  # the healthy parent's  # noqa: PLR2004 - counted spans
            assert [(s.branch_id, s.code, s.head_seq) for s in first.skipped] == [
                (CHILD, "log_corrupt", 5)
            ]
            exporter = otel(store=store, endpoint=c.url)
            await exporter.sync()
            reads.clear()
            again = await exporter.sync()  # within the back-off: not read again
            assert isinstance(again, Ok)
            assert again.value.skipped == ()
            assert CHILD not in reads
            await _cut(store, CHILD, 4)  # its head moved: read again at once
            moved = await exporter.sync()
            assert isinstance(moved, Ok)
            assert [(s.branch_id, s.head_seq) for s in moved.value.skipped] == [(CHILD, 4)]

    asyncio.run(main())


def test_a_crashed_branch_is_read_once_until_something_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crashed turn nobody recovers is read once, then not again until something appends."""
    reads = _spy(monkeypatch)

    async def main() -> None:
        async with collector() as c:
            store = sqlite(str(tmp_path / "s"))
            await imported(store, "otel-turn-model-tool", through={PARENT: 5})
            assert (await _sync(store, c)).spans == 1  # the first chat span
            assert (await _sync(store, c)).spans == 0
            assert reads == [PARENT]

    asyncio.run(main())
