"""open_thread and the Thread handle: timeline, fork points, fork into an isolated sandbox, and
save_case (spec/api.json `Thread`; F11.1, F13.1-F13.3)."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path

from corpus import Clock
from kit import USER, Tools, allow_all, text
from pydantic import JsonValue
from sandbox_kit import OPEN
from schema_check import CASE_ID, valid

from threads.agents.store import HOLDER, Store, now_ms, open_store, sqlite
from threads.evals.run import run_evals
from threads.log import BranchId, ForkEvent, SnapshotEvent, ThreadId
from threads.loop.drafts import draft
from threads.loop.drive import drive
from threads.loop.runtime import Runtime, serving
from threads.loop.scripted import scripted_model
from threads.result import Err, Ok
from threads.sandbox import FakeSandbox, SandboxSession, fake_sandbox
from threads.store import ForkRequest, SqliteStore, Writer, verify_export
from threads.store.lines import uuid7
from threads.thread.case import CaseExpectation
from threads.thread.handle import Thread, open_thread
from threads.thread.snapshot import take_snapshot

STARTED: dict[str, JsonValue] = {
    "agent_name": "test",
    "config_hash": "0" * 64,
    "instructions": "Test.",
    "model": {"provider": "scripted", "name": "scripted-1"},
    "model_params": {"max_tokens": 1024},
    "adapter": {"name": "scripted", "version": "1", "settings": {}},
    "tools": [],
}


@dataclass(frozen=True, slots=True)
class World:
    store: Store
    sq: SqliteStore
    writer: Writer
    sandbox: FakeSandbox
    session: SandboxSession
    thread_id: ThreadId


async def world() -> World:
    """A thread whose sandbox holds /workspace/a.txt = v1, started and quiescent."""
    store = sqlite(":memory:")
    sq = await open_store(store)
    thread, branch = ThreadId(uuid7(now_ms())), BranchId(uuid7(now_ms()))
    assert await sq.create(thread, branch, now_ms()) == Ok(None)
    writer = await sq.acquire(branch, "parent", now_ms)
    assert isinstance(writer, Ok)
    assert isinstance(await writer.value.append([draft("thread_started", STARTED)]), Ok)
    sandbox = fake_sandbox()
    made = await sandbox.create("parent-sandbox", OPEN)
    assert isinstance(made, Ok)
    assert await made.value.upload("/workspace/a.txt", b"v1", OPEN) == Ok(None)
    return World(store, sq, writer.value, sandbox, made.value, thread)


def run(body: Callable[[World], Awaitable[None]]) -> None:
    async def main() -> None:
        w = await world()
        try:
            await body(w)
        finally:
            await w.sq.close()

    asyncio.run(main())


async def snapshot(w: World) -> SnapshotEvent:
    taken = await take_snapshot(w.sq, w.writer, w.sandbox, w.session, now_ms)
    assert isinstance(taken, Ok), taken
    return taken.value


async def opened(
    w: World, *, sandbox: FakeSandbox | None = None, branch_id: BranchId | None = None
) -> Thread:
    got = await open_thread(w.store, w.thread_id, branch_id=branch_id, sandbox=sandbox)
    assert isinstance(got, Ok), got
    return got.value


def test_fork_restores_an_isolated_sandbox_and_the_child_runs() -> None:
    async def body(w: World) -> None:
        snap = await snapshot(w)
        thread = await opened(w, sandbox=w.sandbox)
        timeline = await thread.timeline()
        assert isinstance(timeline, Ok)
        assert [(e.event.type, e.fork_point) for e in timeline.value.entries] == [
            ("thread_started", False),
            ("snapshot", True),
        ]
        points = await thread.fork_points()
        assert isinstance(points, Ok)
        assert [p.event_id for p in points.value] == [snap.event_id]
        child = await thread.fork(points.value[0])
        assert isinstance(child, Ok), child
        read = await w.sq.read(child.value.branch, now_ms())
        assert isinstance(read, Ok)
        fork = read.value.fold.events[-1]
        assert isinstance(fork, ForkEvent)
        assert fork.data.knowledge_policy == "pinned"
        restored = await w.sandbox.attach(str(fork.data.sandbox_id), OPEN)
        assert isinstance(restored, Ok)
        assert await restored.value.download("/workspace/a.txt", OPEN) == Ok(b"v1")
        assert await restored.value.upload("/workspace/a.txt", b"v2", OPEN) == Ok(None)
        assert await w.session.download("/workspace/a.txt", OPEN) == Ok(b"v1")
        # The image's verification sandbox was ledgered and released.
        assert [(r.kind, r.state) for r in await w.sq.ledger.rows()] == [
            ("snapshot", "live"),
            ("sandbox", "released"),
            ("sandbox", "live"),
        ]
        # The fork handed the child's lease back: a run, its own holder, takes it at once.
        assert isinstance(await w.sq.acquire(child.value.branch, "a-run", now_ms), Ok)

    run(body)


def test_fork_without_a_sandbox_adapter_creates_nothing() -> None:
    async def body(w: World) -> None:
        snap = await snapshot(w)
        thread = await opened(w)
        refused = await thread.fork(snap.event_id)
        assert isinstance(refused, Err)
        assert (refused.error.code, refused.error.seq) == ("sandbox_required", snap.seq)
        # The snapshot's image verification, and nothing for the fork.
        assert (w.sandbox.creates, w.sandbox.releases) == (2, 1)
        assert [r.kind for r in await w.sq.ledger.rows()] == ["snapshot", "sandbox"]

    run(body)


def test_no_snapshot_while_a_turn_is_open() -> None:
    async def body(w: World) -> None:
        user = replace(draft("user_input", {"source": "api", "text": "go"}), actor=USER)
        assert isinstance(await w.writer.append([user]), Ok)
        refused = await take_snapshot(w.sq, w.writer, w.sandbox, w.session, now_ms)
        assert isinstance(refused, Err)
        assert refused.error.code == "not_quiescent"
        assert await w.sq.ledger.rows() == ()

    run(body)


def test_a_repair_child_is_inspection_only_but_forks_at_a_snapshot() -> None:
    async def body(w: World) -> None:
        snap = await snapshot(w)
        repair = BranchId(uuid7(now_ms()))
        request = ForkRequest(w.writer.branch_id, snap.seq, repair, {"reason": "repair"})
        assert await w.sq.fork(request, "operator", now_ms) == Ok(None)
        refused = await w.sq.acquire(repair, HOLDER, now_ms)
        assert isinstance(refused, Err)
        assert refused.error.code == "branch_not_runnable"
        handle = await opened(w, branch_id=repair, sandbox=w.sandbox)
        child = await handle.fork(snap.event_id)
        assert isinstance(child, Ok), child
        assert isinstance(await w.sq.acquire(child.value.branch, HOLDER, now_ms), Ok)

    run(body)


async def turn(w: World, reply: str) -> None:
    """One recorded turn after the snapshot: the input and the model's reply."""
    clock = Clock(now_ms())
    model = scripted_model({"responses": [text(reply)]})
    rt = Runtime(
        w.sq, w.writer, serving(model), Tools({}, clock), allow_all, now_ms, clock.wait_until
    )
    user = replace(draft("user_input", {"source": "api", "text": "again"}), actor=USER)
    assert isinstance(await rt.append(user), Ok)
    await drive(rt)


def test_save_case_writes_a_stub_case_the_runner_replays(tmp_path: Path) -> None:
    async def body(w: World) -> None:
        await snapshot(w)
        await turn(w, "Hi.")
        thread = await opened(w, sandbox=w.sandbox)
        expect = CaseExpectation(must=({"type": "turn_completed"},))
        saved = await thread.save_case(
            "says-hi", expect=expect, external_effects="stub", dir=str(tmp_path)
        )
        assert isinstance(saved, Ok), saved
        folder = Path(saved.value.path)
        meta = json.loads((folder / "case.json").read_bytes())
        assert valid(meta, f"{CASE_ID}#/$defs/Case")
        assert (meta["kind"], meta["input"]) == ("stub", {"text": "again"})
        for name, schema in [("model.json", "ModelScript"), ("stubs.json", "StubScript")]:
            assert valid(json.loads((folder / name).read_bytes()), f"{CASE_ID}#/$defs/{schema}")
        for impl in ("threads-py", "threads-ts"):
            log = verify_export((folder / f"log.{impl}.jsonl").read_bytes(), now_ms())
            assert isinstance(log, Ok), log
            assert log.value.segments[-1].header.writer.impl == impl
            expected = json.loads((folder / f"expected.{impl}.json").read_bytes())
            assert valid(expected, f"{CASE_ID}#/$defs/Expected")
            assert expected["state"] == log.value.state.to_json()
        # The eval runner reruns it: input, the recorded reply, and the assertion.
        report = await run_evals(cases=str(tmp_path))
        assert [(c.name, c.status) for c in report.cases] == [("says-hi", "passed")]
        # The snapshot sits right before the turn's run: a live run may restore it.
        assert meta["snapshot"]["provider"] == "fake"
        refused = await thread.save_case(
            "no-assertion",
            expect=CaseExpectation(must=()),
            external_effects="stub",
            dir=str(tmp_path),
        )
        assert isinstance(refused, Err)
        assert refused.error.code == "invalid_request"

    run(body)


def test_save_case_needs_a_completed_turn(tmp_path: Path) -> None:
    async def body(w: World) -> None:
        thread = await opened(w, sandbox=w.sandbox)
        expect = CaseExpectation(must=({"type": "snapshot"},))
        refused = await thread.save_case(
            "c", expect=expect, external_effects="stub", dir=str(tmp_path)
        )
        assert isinstance(refused, Err)
        assert refused.error.code == "invalid_request"
        assert list(tmp_path.iterdir()) == []

    run(body)


def test_open_thread_refuses_an_unknown_thread() -> None:
    async def body(w: World) -> None:
        missing = await open_thread(w.store, ThreadId(uuid7(now_ms())))
        assert isinstance(missing, Err)
        assert missing.error.code == "not_found"

    run(body)
