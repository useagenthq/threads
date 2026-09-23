"""Job `schedule-two-schedulers-one-run`: two host processes
fire the same schedule's occurrences at once on one store. Each occurrence is claimed once, so
it fires once and starts one run."""

import asyncio
import json
from collections import Counter
from pathlib import Path

import pytest
from jobs.drill import finish, pids, spawn
from jobs.worker import rows

from threads.log import ScheduleFiredEvent, UserInputEvent
from threads.result import Ok
from threads.store import SqliteStore

pytestmark = pytest.mark.jobs

MINUTE_MS = 60_000
START = 1_790_000_040_000  # a minute boundary
OCCURRENCES = [START + i * MINUTE_MS for i in range(12)]


def test_two_schedulers_run_each_occurrence_once(tmp_path: Path) -> None:
    schedulers = [
        spawn("schedule", tmp_path, DRILL_OCCURRENCES=json.dumps(OCCURRENCES)) for _ in range(2)
    ]
    (tmp_path / "go").touch()
    for scheduler in schedulers:
        finish(scheduler)

    claims = Counter(int(str(r["at"])) for r in rows(tmp_path / "claims.jsonl"))
    assert claims == Counter(OCCURRENCES)
    assert len(rows(tmp_path / "model.jsonl")) == len(OCCURRENCES)
    fired, inputs = asyncio.run(_fired(tmp_path))
    assert sorted(fired) == OCCURRENCES
    assert inputs == len(OCCURRENCES)
    # A claim and its run belong to one process: runs started only where claims were won.
    assert pids(tmp_path, "model.jsonl") <= pids(tmp_path, "claims.jsonl")


async def _fired(where: Path) -> tuple[list[int], int]:
    """Every schedule_fired's occurrence, and the user_inputs, across the store's threads."""
    opened = await SqliteStore.open(where / "threads.db")
    assert isinstance(opened, Ok)
    sq = opened.value
    try:
        threads = await sq.run(
            lambda c: c.execute("SELECT thread_id FROM schedule_occurrences").fetchall()
        )
        fired: list[int] = []
        inputs = 0
        for (thread,) in threads:
            root = await sq.root(thread)
            assert isinstance(root, Ok)
            log = await sq.read(root.value, 0)
            assert isinstance(log, Ok)
            events = log.value.fold.events
            fired += [e.data.scheduled_for for e in events if isinstance(e, ScheduleFiredEvent)]
            inputs += sum(isinstance(e, UserInputEvent) for e in events)
        return fired, inputs
    finally:
        await sq.close()
