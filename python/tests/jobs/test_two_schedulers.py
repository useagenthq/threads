"""Job `schedule-two-schedulers-one-run`: two host processes tick the same schedule over the same
due occurrences on one store. Each occurrence is decided once and runs at most once."""

import asyncio
import json
from pathlib import Path

import pytest
from jobs.drill import finish, spawn
from jobs.stores import drill_open
from jobs.worker import rows

from threads.log import (
    Event,
    ScheduleFiredEvent,
    ScheduleSkippedEvent,
    ThreadId,
    ThreadStartedEvent,
    UserInputEvent,
)
from threads.result import Ok
from threads.store.sql import int_of, text_of

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

    decided, events = asyncio.run(_read(tmp_path))
    # Each occurrence is decided exactly once, by one scheduler.
    assert [at for at, _ in decided] == OCCURRENCES
    fired = [e.data.scheduled_for for e in events if isinstance(e, ScheduleFiredEvent)]
    skipped = [e.data.reason for e in events if isinstance(e, ScheduleSkippedEvent)]
    # One fired an occurrence while the other's run still held the thread: an overlap, run never.
    assert fired == [at for at, state in decided if state == "fired"]
    assert set(skipped) <= {"overlap"}
    assert len(fired) + len(skipped) == len(OCCURRENCES)
    assert fired
    # Every fired occurrence ran exactly once: one input, one model call, across both processes.
    assert sum(isinstance(e, UserInputEvent) for e in events) == len(fired)
    assert len(rows(tmp_path / "model.jsonl")) == len(fired)
    assert sum(isinstance(e, ThreadStartedEvent) for e in events) == 1


async def _read(where: Path) -> tuple[list[tuple[int, str]], list[Event]]:
    """The decided occurrences, and the schedule's one thread read back from the log."""
    opened = await drill_open(where, "local")
    assert isinstance(opened, Ok)
    sq = opened.value
    try:
        decided = await sq.run(
            lambda c: c.execute(
                "SELECT occurrence_at, state FROM schedule_occurrences ORDER BY occurrence_at"
            ).fetchall()
        )
        found = await sq.run(
            lambda c: c.execute("SELECT thread_id FROM schedule_threads").fetchall()
        )
        ((thread,),) = found
        root = await sq.root(ThreadId(text_of(thread)))
        assert isinstance(root, Ok)
        log = await sq.read(root.value, 0)
        assert isinstance(log, Ok)
        return [(int_of(at), text_of(state)) for at, state in decided], list(log.value.fold.events)
    finally:
        await sq.close()
