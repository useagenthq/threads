"""C5: a value registered after a check on the event loop but before the store's thread
writes what it guarded. Each write re-checks with registration paused."""

import asyncio
import sqlite3
import threading
from collections.abc import Callable, Mapping

import pytest
from pydantic import JsonValue
from redaction_kit import RACED, ROOT, T0, Race, opened, started, user, writer

from threads.log import BranchId, ParseError
from threads.log.digest import sha256_hex
from threads.memory import local_knowledge
from threads.memory.conformance import A
from threads.memory.local_knowledge import LocalKnowledge
from threads.memory.types import Binding, KnowledgeSource
from threads.redaction import SecretInStoredBytesError, register
from threads.result import Err, Ok
from threads.store import Draft, ForkRequest, SqliteStore, verify_export
from threads.store import branches as store_branches
from threads.store.forking import ChildStart, Forking
from threads.store.worker import Worker


def test_a_value_registered_before_the_append_publishes_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The append's drafts are admitted on the store's thread with registration paused, so a
    value registered while the append waited for that thread is redacted like any other."""

    async def main() -> None:
        store = await opened()
        w = await writer(store)
        assert isinstance(await w.append([started({})]), Ok)
        Race(monkeypatch, RACED)
        appended = await w.append([user(f"note {RACED}")])
        assert isinstance(appended, Ok)
        exported = await store.export(ROOT)
        assert isinstance(exported, Ok)
        assert RACED.encode() not in exported.value
        # The redacted note opened a turn: the writer goes on and ends it.
        assert isinstance(await w.append([Draft("turn_completed", {"reason": "end_turn"})]), Ok)
        await store.close()

    asyncio.run(main())


def test_a_value_registered_before_the_spill_commits_drops_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def main() -> None:
        store = await opened()
        spill = await store.spill()
        await spill.write(RACED.encode())
        # The end-of-stream flush is the first statement; the commit is the second.
        Race(monkeypatch, RACED, at=2)
        assert await spill.commit() is None
        assert isinstance(await store.get_artifact(sha256_hex(RACED.encode())), Err)
        await store.close()

    asyncio.run(main())


def test_a_value_registered_before_an_artifact_publishes_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = f"text {RACED}".encode()

    async def main() -> None:
        store = await opened()
        Race(monkeypatch, RACED)
        with pytest.raises(SecretInStoredBytesError):
            await store.put_artifact(data)
        assert isinstance(await store.get_artifact(sha256_hex(data)), Err)
        await store.close()

    asyncio.run(main())


def test_a_value_registered_before_an_import_publishes_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def main() -> None:
        store = await opened()
        w = await writer(store)
        assert isinstance(await w.append([started({}), user(f"hi {RACED}")]), Ok)
        exported = await store.export(ROOT)
        assert isinstance(exported, Ok)
        verified = verify_export(exported.value, T0)
        assert isinstance(verified, Ok)
        fresh = await opened()
        Race(monkeypatch, RACED)
        imported = await fresh.import_log(verified.value)
        assert isinstance(imported, Err)
        assert imported.error.code == "secret_in_stored_bytes"
        await store.close()

    asyncio.run(main())


def test_a_value_registered_before_a_fork_event_publishes_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = BranchId("0192b000-0000-7000-8000-000000000002")
    snapshot = Draft(
        "snapshot",
        {
            "snapshot_id": "snap_01",
            "provider": "fake",
            "sandbox_id": "sbx_parent_01",
            "capture_class": "filesystem",
            "expires_at": None,
            "manifest_hash": "1" * 64,
            "quiesced": {"frozen": [], "stopped": [], "excluded": []},
        },
    )
    built = store_branches.start_child

    def racing(
        started: Forking, data: Mapping[str, JsonValue], now: int
    ) -> Ok[ChildStart] | Err[ParseError]:
        made = built(started, data, now)
        register(RACED, "raced")
        return made

    async def main() -> None:
        store = await opened()
        w = await writer(store)
        done = Draft("turn_completed", {"reason": "end_turn"})
        assert isinstance(await w.append([started({}), user("hi"), done, snapshot]), Ok)
        monkeypatch.setattr(store_branches, "start_child", racing)
        data: dict[str, JsonValue] = {
            "reason": "snapshot",
            "sandbox_id": RACED,
            "knowledge_policy": "pinned",
        }
        forked = await store.fork(ForkRequest(ROOT, 4, child, data), "c", lambda: T0)
        assert isinstance(forked, Err)
        assert forked.error.code == "secret_in_stored_bytes"
        exported = await store.export(child)
        assert not (isinstance(exported, Ok) and RACED.encode() in exported.value)
        await store.close()

    asyncio.run(main())


def test_a_value_registered_before_a_knowledge_source_publishes_is_an_error_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def main() -> None:
        store = await opened()
        provider = await LocalKnowledge(()).bind(store)
        source = KnowledgeSource(
            source_id="doc.md",
            media_type="text/markdown",
            content=f"the note {RACED}".encode(),
            binding=Binding(namespace="n", record_id="doc"),
        )
        Race(monkeypatch, RACED)
        got = await provider.ingest(A, source, "doc@1")
        assert isinstance(got, Err)
        assert got.error.code == "invalid"
        await store.close()

    asyncio.run(main())


def test_a_value_registered_before_a_knowledge_source_is_indexed_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The source is saved and indexed in one handoff to the store's thread, so there is no
    second handoff where a value registered meanwhile could reach the index."""

    async def main() -> None:
        store = await opened()
        provider = await LocalKnowledge(()).bind(store)
        source = KnowledgeSource(
            source_id="doc.md",
            media_type="text/markdown",
            content=f"the note {RACED}".encode(),
            binding=Binding(namespace="n", record_id="doc"),
        )
        race = Race(monkeypatch, RACED)
        got = await provider.ingest(A, source, "doc@1")
        assert race.left == 0, "ingest hands the store exactly one statement"
        assert isinstance(got, Err)
        assert got.error.code == "invalid"
        assert isinstance(await store.get_artifact(sha256_hex(source.content)), Err)
        indexed = await store.run(
            lambda c: c.execute("SELECT count(*) FROM local_knowledge_fts").fetchone()
        )
        assert indexed == (0,)
        await store.close()

    asyncio.run(main())


def test_a_rebuild_over_a_source_holding_a_value_keeps_the_old_index() -> None:
    async def main() -> None:
        store = await opened()
        provider = await LocalKnowledge(()).bind(store)
        for name in ("a.md", "b.md"):
            source = KnowledgeSource(
                source_id=name,
                media_type="text/markdown",
                content=f"{name} mentions {RACED}".encode(),
                binding=Binding(namespace="n", record_id=name),
            )
            assert isinstance(await provider.ingest(A, source, name), Ok)

        def count(c: sqlite3.Connection) -> tuple[int]:
            return c.execute("SELECT count(*) FROM local_knowledge_fts").fetchone()

        before = await store.run(count)
        register(RACED, "later")
        rebuilt = await provider.rebuild_index()
        assert isinstance(rebuilt, Err)
        assert rebuilt.error.code == "invalid"
        assert await store.run(count) == before
        await store.close()

    asyncio.run(main())


def test_a_cancelled_append_whose_value_was_registered_meanwhile_leaves_the_writer_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def main() -> None:
        store = await opened()
        w = await writer(store)
        assert isinstance(await w.append([started({})]), Ok)
        original = Worker.call
        appending: list[asyncio.Task[object]] = []

        async def call[T](worker: Worker, statement: Callable[[sqlite3.Connection], T]) -> T:
            if appending:
                register(RACED, "raced")
                appending.pop().cancel()
            return await original(worker, statement)

        monkeypatch.setattr(Worker, "call", call)
        task: asyncio.Task[object] = asyncio.ensure_future(w.append([user(f"note {RACED}")]))
        appending.append(task)
        with pytest.raises(asyncio.CancelledError):
            await task
        # The redacted note opened a turn: the writer goes on and ends it.
        assert isinstance(await w.append([Draft("turn_completed", {"reason": "end_turn"})]), Ok)
        exported = await store.export(ROOT)
        assert isinstance(exported, Ok)
        assert RACED.encode() not in exported.value
        await store.close()

    asyncio.run(main())


def test_a_source_admitted_while_the_index_rebuilds_stays_searchable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rebuild that read its sources before another ingest committed must not drop it."""

    def source(name: str, text: str) -> KnowledgeSource:
        return KnowledgeSource(
            source_id=name,
            media_type="text/markdown",
            content=text.encode(),
            binding=Binding(namespace="n", record_id=name),
        )

    async def main() -> None:
        store = await opened()
        provider = await LocalKnowledge(()).bind(store)
        assert isinstance(await provider.ingest(A, source("a.md", "alpha first"), "a"), Ok)
        read = SqliteStore.get_artifact
        late: list[KnowledgeSource] = [source("b.md", "zebra second")]

        async def reading(self: SqliteStore, sha256: str) -> Ok[bytes] | Err[ParseError]:
            if late:
                assert isinstance(await provider.ingest(A, late.pop(), "b"), Ok)
            return await read(self, sha256)

        monkeypatch.setattr(SqliteStore, "get_artifact", reading)
        assert await provider.rebuild_index() == Ok(None)
        found = await provider.search(A, "zebra")
        assert isinstance(found, Ok)
        assert [hit.doc_id for hit in found.value] == ["b.md"]
        await store.close()

    asyncio.run(main())


def test_registration_waits_until_the_rebuilt_index_is_committed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value registered from another thread while the rebuild writes must wait for its COMMIT:
    the check that let the refill run holds until the index is durable."""
    events: list[str] = []
    begin = local_knowledge.transaction

    def transaction[T](
        body: Callable[[sqlite3.Connection], T],
    ) -> Callable[[sqlite3.Connection], T]:
        """The rebuild's transaction, with a registration started on another thread as it
        opens, and its COMMIT recorded after giving that registration every chance to land."""

        def run(conn: sqlite3.Connection) -> T:
            registering = threading.Thread(
                target=lambda: (register(RACED, "late"), events.append("registered"))
            )

            def traced(statement: str) -> None:
                if statement == "COMMIT":
                    registering.join(timeout=0.5)
                    events.append("COMMIT")

            conn.set_trace_callback(traced)
            registering.start()
            return begin(body)(conn)

        return run

    async def main() -> None:
        store = await opened()
        provider = await LocalKnowledge(()).bind(store)
        source = KnowledgeSource(
            source_id="a.md",
            media_type="text/markdown",
            content=f"alpha {RACED}".encode(),
            binding=Binding(namespace="n", record_id="a"),
        )
        assert isinstance(await provider.ingest(A, source, "a"), Ok)
        monkeypatch.setattr(local_knowledge, "transaction", transaction)
        assert await provider.rebuild_index() == Ok(None)
        await store.run(lambda c: c.set_trace_callback(None))
        await store.close()

    asyncio.run(main())
    deadline = 50
    while "registered" not in events and deadline:
        deadline -= 1
        threading.Event().wait(0.1)
    assert events.index("COMMIT") < events.index("registered"), events
