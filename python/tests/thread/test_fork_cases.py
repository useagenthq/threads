"""Conformance runner for the `fork` cases (spec/conformance/README.md, "What a runner does per
kind"): import the log, fork at the case's event against the fake sandbox, and compare the
child's lines, the fork facts, the ledger rows and the untouched parent."""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from corpus import CASES, cases, load, matches, now_of, obj, stored_artifacts
from pydantic import JsonValue

from threads.log import BranchId, EventId, ParseError
from threads.reduce.handlers import to_json
from threads.result import Err, Ok
from threads.sandbox import FakeSandbox, fake_sandbox
from threads.sandbox.fake import FakeCrashError
from threads.store import SqliteStore, StoredEvent, Writer, verify_export
from threads.thread.fork import (
    ForkAt,
    KnowledgePolicy,
    fork_branch,
    knowledge_revision,
    recover_forks,
)


@dataclass(frozen=True, slots=True)
class Outcome:
    error: ParseError | None
    state: JsonValue
    parent_unchanged: bool
    child_created: bool
    child_state: str | None
    child_events: tuple[StoredEvent, ...]
    at_seq: int | None
    knowledge_revision: int | None
    creates: int
    rows: tuple[tuple[str, str], ...]


def knowledge(value: JsonValue) -> KnowledgePolicy:
    match value:
        case "current":
            return "current"
        case "pinned" | None:
            return "pinned"
        case _:
            raise ValueError(f"knowledge_policy {value!r}")


async def run_case(case: Path, script: dict[str, JsonValue] | None = None) -> Outcome:
    meta = load(case, "case.json")
    now, request = now_of(meta), obj(meta["input"])
    log = (case / "log.jsonl").read_bytes()
    verified = verify_export(log, now)
    assert isinstance(verified, Ok), verified
    opened = await SqliteStore.open(artifacts=stored_artifacts(case))
    assert isinstance(opened, Ok)
    store = opened.value
    try:
        assert await store.import_log(verified.value) == Ok(None)
        parent = verified.value.segments[-1].header.branch_id
        child = BranchId(str(request["new_branch_id"]))
        if script is None:
            script = load(case, "sandbox.json") if "sandbox_script" in meta else {}
        sandbox = fake_sandbox(script)
        point = EventId(str(request["fork_at_event_id"]))
        at = ForkAt(parent, point, child, knowledge(request.get("knowledge_policy")))
        forked = await _fork(store, sandbox, at, now)
        read = await store.read(parent, now)
        assert isinstance(read, Ok)
        row = await store.branch(child)
        state = row.value.state if isinstance(row, Ok) else None
        created = state in ("ready", "inspection_only")
        events, at_seq, revision = (), None, None
        if created:
            events, at_seq, revision = await _child(case, store, child, now)
        return Outcome(
            forked.error if isinstance(forked, Err) else None,
            read.value.state.to_json(),
            await store.export(parent) == Ok(log),
            created,
            state,
            events,
            at_seq,
            revision,
            sandbox.creates,
            tuple((r.kind, r.state) for r in await store.ledger.rows()),
        )
    finally:
        await store.close()


async def _fork(
    store: SqliteStore, sandbox: FakeSandbox, at: ForkAt, now: int
) -> Ok[Writer] | Err[ParseError] | None:
    """The fork; for restore_response crash, the host dies mid-fork, restarts and recovers."""
    try:
        return await fork_branch(store, sandbox, at, "conformance", lambda: now)
    except FakeCrashError:
        assert await recover_forks(store, sandbox, "conformance", lambda: now) == (at.child,)
        return None


async def _child(
    case: Path, store: SqliteStore, child: BranchId, now: int
) -> tuple[tuple[StoredEvent, ...], int | None, int | None]:
    """The child's own lines. Its export imports cleanly into a fresh store."""
    exported = await store.export(child)
    assert isinstance(exported, Ok)
    read = verify_export(exported.value, now)
    assert isinstance(read, Ok), read
    fresh = await SqliteStore.open(artifacts=stored_artifacts(case))
    assert isinstance(fresh, Ok)
    assert await fresh.value.import_log(read.value) == Ok(None)
    await fresh.value.close()
    own = read.value.segments[-1]
    events = tuple(event for event, _ in own.events)
    return events, own.fork_at_seq, knowledge_revision(read.value.fold.events)


@pytest.mark.parametrize("name", cases("fork"))
def test_fork_case(name: str) -> None:
    case = CASES / name
    expected = load(case, "expected.json")
    got = asyncio.run(run_case(case))
    if expected["outcome"] == "error":
        assert got.error is not None
        assert {"code": got.error.code, "seq": got.error.seq} == expected["error"]
        # Nothing is left to clean up but a row whose outcome can't be established.
        assert all(state in ("released", "unknown") for _, state in got.rows)
    else:
        assert got.error is None, got.error
    if "state" in expected:
        assert got.state == expected["state"]
    matchers = expected.get("appended", [])
    assert isinstance(matchers, list)
    wire = [json.loads(json.dumps(to_json(e))) for e in got.child_events]
    assert len(got.child_events) == len(matchers), wire
    for matcher, event in zip(matchers, got.child_events, strict=True):
        assert matches(obj(matcher), event), (matcher, to_json(event))
    fork = obj(expected["fork"])
    assert got.parent_unchanged is fork["parent_unchanged"]
    assert got.child_created is fork["child_created"]
    if "at_seq" in fork:
        assert got.at_seq == fork["at_seq"]
    if "child_state" in fork:
        assert got.child_state == fork["child_state"]
    if "knowledge_revision" in fork:
        assert got.knowledge_revision == fork["knowledge_revision"]
    if "resources" in expected:
        resources = obj(expected["resources"])
        assert got.creates == resources["creates"]
        rows = resources["rows"]
        assert isinstance(rows, list)
        assert list(got.rows) == [(obj(r)["kind"], obj(r)["state"]) for r in rows]


def test_an_interrupted_fork_never_calls_an_unproven_resource_released() -> None:
    """recovery parks a restore it can't find by key as unknown; only a final
    not_found or a confirmed release counts as released."""
    case = CASES / "fork-crash-no-orphan"
    snap = obj(obj(load(case, "sandbox.json")["snapshots"])["snap_01"])
    unprovable: dict[str, JsonValue] = {
        "snapshots": {"snap_01": {**snap, "create_lookup": "unsupported"}}
    }
    got = asyncio.run(run_case(case, unprovable))
    assert (got.child_state, got.rows) == ("fork_failed", (("sandbox", "unknown"),))
