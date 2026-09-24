"""An import stores bytes without its appends' hooks, so it folds the index again in the same
transaction: the imported branches' wake rows, and every team the log belongs to. A schedule's
new root runs the hooks too."""

import asyncio
import json

from pydantic import JsonValue
from team.team_kit import (
    CASES,
    LEAD,
    TEAM,
    TEAM_LOG,
    TENANT,
    assert_team_replays,
    branch_of,
    index_rows,
    verified,
)
from team.writes import ReplayClock, draft_of

from threads.log import BranchId, ThreadId
from threads.result import Ok
from threads.store import MemoryArtifacts, SqliteStore
from threads.store.opening import insert_root, new_root

WAKE = CASES / "legacy-wake-pending-row"


def test_a_background_child_still_running_has_its_wake_row() -> None:
    async def main() -> list[tuple[object, ...]]:
        artifacts = MemoryArtifacts()
        for path in (WAKE / "artifacts").iterdir():
            artifacts.put(path.read_bytes())
        opened = await SqliteStore.open(artifacts=artifacts)
        assert isinstance(opened, Ok)
        log = verified((WAKE / "log.jsonl").read_bytes())
        assert isinstance(log, Ok)
        assert await opened.value.import_log(log.value) == Ok(None)
        return await opened.value.run(
            lambda c: c.execute("SELECT branch_id, child_thread_id FROM pending_wakes").fetchall()
        )

    expected = json.loads((WAKE / "expected.json").read_text())["projections"]["pending_wakes"]
    assert asyncio.run(main()) == [(r["branch_id"], r["child_thread_id"]) for r in expected]


def _without_feed(rows: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {k: v for k, v in rows.items() if k != "team_feed"}


def test_a_team_imported_log_by_log_has_the_rows_its_appends_wrote() -> None:
    async def main() -> None:
        written = await SqliteStore.open(tenant_id=TENANT)
        imported = await SqliteStore.open(tenant_id=TENANT)
        assert isinstance(written, Ok)
        assert isinstance(imported, Ok)
        log = verified((CASES / "team-settle-wakes-lead" / "logs" / "lead.jsonl").read_bytes())
        assert isinstance(log, Ok)
        drafts = [draft_of(e) for e in log.value.fold.events[:2]]
        clock = ReplayClock(1_790_000_000_000)
        opened = await written.value.open_branch(
            LEAD, branch_of(LEAD), drafts, holder_id="lead", clock=clock
        )
        assert isinstance(opened, Ok)
        for branch in (branch_of(LEAD), branch_of(TEAM_LOG)):
            exported = await written.value.export(branch)
            assert isinstance(exported, Ok)
            read = verified(exported.value)
            assert isinstance(read, Ok)
            assert await imported.value.import_log(read.value) == Ok(None)
        assert _without_feed(await imported.value.run(index_rows)) == _without_feed(
            await written.value.run(index_rows)
        )
        await assert_team_replays(imported.value, TEAM)

    asyncio.run(main())


def test_a_new_root_that_names_a_team_opens_its_team_log() -> None:
    async def main() -> None:
        opened = await SqliteStore.open(tenant_id=TENANT)
        assert isinstance(opened, Ok)
        store = opened.value
        log = verified((CASES / "team-settle-wakes-lead" / "logs" / "lead.jsonl").read_bytes())
        assert isinstance(log, Ok)
        started = draft_of(log.value.fold.events[0])
        root = new_root(TENANT, LEAD, BranchId(branch_of(LEAD)), started, 1_790_000_000_000)
        assert isinstance(root, Ok)
        assert await store.run(lambda c: insert_root(c, root.value)) is None
        team_log = await store.read(branch_of(TEAM_LOG), 1_790_000_000_000)
        assert isinstance(team_log, Ok)
        assert team_log.value.segments[0].header.thread_id == ThreadId(TEAM_LOG)
        await assert_team_replays(store, TEAM)

    asyncio.run(main())
