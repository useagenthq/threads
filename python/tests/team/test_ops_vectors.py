"""The op vectors this build runs (spec/conformance/vectors/team-ops.json): each op on its world
through this runtime's own store ops reaches the reference's outcome, appends the same event types
per log and makes the same row changes; the team then replays. The same selection as TypeScript's
test/team/ops.test.ts."""

import asyncio
from collections.abc import Callable

import pytest
from pydantic import JsonValue
from team.op_run import run_on
from team.team_kit import assert_team_replays
from team.vectors import (
    TEAM,
    Obj,
    changes,
    obj,
    rows,
    seeded,
    vector_mint,
    vectors,
    world_logs,
)

from threads.log import BranchId, MailEnvelope, MemberStartedEvent
from threads.result import Err, Ok
from threads.store import SqliteStore, Writer
from threads.team.materialize import MaterializeOptions, Rebind, materialize

MINE = [v for v in vectors() if "lane" not in v]
"""A vector tagged for a later lane waits for that sub-lane's build."""


def _clock(v: Obj) -> Callable[[], int]:
    now = v["now"]
    assert isinstance(now, int)
    return lambda: now


async def _writer(store: SqliteStore, v: Obj) -> Writer:
    branch = BranchId(str(world_logs(v)[str(v["by"])]["branch_id"]))
    got = await store.acquire(branch, "vectors", _clock(v))
    assert isinstance(got, Ok), got
    return got.value


async def _materialize(store: SqliteStore, v: Obj) -> JsonValue:
    inp = obj(v["input"])
    status = inp["rebind"]
    assert status in ("ok", "pin_unavailable", "pin_mismatch", "setup_failed")

    async def rebind(_started: MemberStartedEvent, _task: MailEnvelope) -> Rebind:
        return Rebind(status)

    o = MaterializeOptions(
        rebind, "vectors", 30_000, _clock(v), vector_mint, BranchId(str(inp["branch_id"]))
    )
    got = await materialize(store, TEAM, str(inp["member"]), o)
    assert isinstance(got, Ok), got
    m = got.value
    if m.status == "rebind_failed":
        return {"status": m.status, "code": m.code}
    return {"status": m.status}


async def _run(store: SqliteStore, v: Obj) -> JsonValue:
    if v["op"] == "materialize":
        return await _materialize(store, v)
    return await run_on(await _writer(store, v), v)


async def _appended(store: SqliteStore, v: Obj, heads: dict[str, int]) -> Obj:
    labels = {label: str(log["branch_id"]) for label, log in world_logs(v).items()}
    inp = obj(v["input"])
    if v["op"] == "materialize":
        labels[str(inp["label"])] = str(inp["branch_id"])
    out: Obj = {}
    for label, branch in labels.items():
        read = await store.read(BranchId(branch), 0)
        if isinstance(read, Err):
            continue
        types: list[JsonValue] = [
            e.type for e in read.value.fold.events if e.seq > heads.get(label, 0)
        ]
        if types:
            out[label] = types
    return out


def test_the_selection_covers_this_builds_ops() -> None:
    assert {str(v["op"]) for v in MINE} == {
        *("start", "send", "ask", "reply", "wait", "monitor", "cancel"),
        *("deadline", "consume"),
        *("materialize", "idle", "end"),
    }
    # Pinned: a vector that drops out of the selection fails here, not silently.
    assert len(MINE) == 107  # noqa: PLR2004 - the pinned selection size
    assert sum(v["by"] == "team" for v in MINE) == 29  # noqa: PLR2004 - of them, the operator's


@pytest.mark.parametrize("v", MINE, ids=[str(v["name"]) for v in MINE])
def test_vector(v: Obj) -> None:
    async def main() -> None:
        store = await seeded(v)
        try:
            before = await store.run(rows)
            heads: dict[str, int] = {}
            for label, log in world_logs(v).items():
                read = await store.read(BranchId(str(log["branch_id"])), 0)
                assert isinstance(read, Ok)
                heads[label] = read.value.fold.seq
            expect = obj(v["expect"])
            assert await _run(store, v) == expect["outcome"]
            assert await _appended(store, v, heads) == expect["appended"]
            assert changes(before, await store.run(rows)) == expect["rows"]
            await assert_team_replays(store, TEAM)
        finally:
            await store.close()

    asyncio.run(main())
