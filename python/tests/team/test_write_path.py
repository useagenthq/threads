"""Write path equals rebuild path: every recorded team (corpus and staged) appended again event
by event through writers into a fresh store leaves exactly the index rows the case expects, and
a wipe and rebuild of each team leaves them again."""

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter
from team.team_kit import CASES, TENANT, assert_team_replays, index_rows, teams_of, verified
from team.writes import reappend

from threads.result import Ok
from threads.store import SqliteStore

STAGED = CASES.parent / "staged"
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def _recorded() -> list[Path]:
    """Every team case that pins an index: its logs are a team's whole write history."""
    staged = STAGED.iterdir() if STAGED.exists() else iter(())
    dirs = sorted([*CASES.iterdir(), *staged])
    return [
        d
        for d in dirs
        if json.loads((d / "case.json").read_text())["kind"] == "team"
        and "index" in json.loads((d / "expected.json").read_text())
    ]


RECORDED = _recorded()


def test_it_covers_every_recorded_team() -> None:
    assert len(RECORDED) >= len(["settle", "rebind", "nested", "operator", "starting", "staged"])


@pytest.mark.parametrize("case", RECORDED, ids=lambda d: d.name)
def test_appends_leave_the_expected_rows_and_a_rebuild_leaves_them_again(case: Path) -> None:
    logs = {p.stem: p.read_bytes() for p in sorted((case / "logs").glob("*.jsonl"))}
    expected = _JSON.validate_json((case / "expected.json").read_bytes())
    assert isinstance(expected, dict)

    async def main() -> None:
        opened = await SqliteStore.open(tenant_id=TENANT)
        assert isinstance(opened, Ok)
        store = opened.value
        reads = [verified(raw) for raw in logs.values()]
        await reappend(store, [r.value for r in reads if isinstance(r, Ok)])
        assert await store.run(index_rows) == expected["index"]
        for team in teams_of(logs):
            await assert_team_replays(store, team)
        assert await store.run(index_rows) == expected["index"]
        await store.close()

    asyncio.run(main())
