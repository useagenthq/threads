"""The two-process races design §7 names, each over many schedules: two processes, each with its
own connection to one store file and its own clock, run one op each at the same moment, and SQLite
serializes their transactions in either order. Every assertion reads log evidence only; no
pre-commit order and no created_at is used. The same schedules as TypeScript's
test/team/race.test.ts."""

import asyncio
import json
import os
import random
import shutil
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Final

import pytest
from jobs.drill import WAIT_S
from pydantic import JsonValue
from team.team_kit import assert_team_replays
from team.vectors import TEAM, Obj, obj, seeded, vectors, world_logs

from threads.log import AskClosedEvent as _Closed
from threads.log import (
    BranchId,
    Event,
    MailRefusedEvent,
    MemberObservedEvent,
    MessageSentEvent,
    ToolResultEvent,
    WaitFinishedEvent,
    WaitStartedEvent,
)
from threads.result import Ok
from threads.store import SqliteStore

pytestmark = pytest.mark.jobs

SCHEDULES: Final = int(os.environ.get("THREADS_RACE_SCHEDULES", "100"))
"""The design's 1,000 schedules are the full jobs run: THREADS_RACE_SCHEDULES=1000."""
WORKER: Final = Path(__file__).with_name("race_worker.py")
TESTS: Final = str(WORKER.parent.parent)
DUE: Final = 1_790_000_220_000
REPLAYED: Final = 0.05
"""The share of schedules that also wipe and rebuild the team index."""


class _Side:
    """A long-lived race process: one job line in, one answer line out."""

    def __init__(self) -> None:
        env = {**os.environ, "PYTHONPATH": TESTS}
        # The worker is this repo's own test script, run by this interpreter.
        self.proc = subprocess.Popen(  # noqa: S603
            [sys.executable, str(WORKER)],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )

    def send(self, job: Obj) -> None:
        stdin = self.proc.stdin
        assert stdin is not None
        stdin.write(json.dumps(job) + "\n")
        stdin.flush()

    def answer(self) -> JsonValue:
        stdout = self.proc.stdout
        assert stdout is not None
        line = stdout.readline()
        assert line, "a race process exited"
        return obj(json.loads(line))["outcome"]

    def close(self) -> None:
        self.proc.kill()
        self.proc.communicate(timeout=WAIT_S)


@pytest.fixture
def sides() -> Iterator[tuple[_Side, _Side]]:
    a, b = _Side(), _Side()
    yield a, b
    a.close()
    b.close()


def _vector(name: str) -> Obj:
    return next(v for v in vectors() if v["name"] == name)


def _template(world: Obj, where: Path) -> Path:
    """The world seeded once into a store file; each schedule starts from a copy."""
    path = where / "template.db"

    async def seed() -> None:
        store = await seeded(world, str(path))
        await store.close()

    asyncio.run(seed())
    return path


def _schedule(
    sides: tuple[_Side, _Side], world: Obj, path: Path, jobs: tuple[Obj, Obj]
) -> dict[str, Sequence[Event]]:
    """One schedule: both sides at once on a fresh copy; the logs after, by label."""
    for side, job in zip(sides, jobs, strict=True):
        side.send({**job, "path": str(path), "spin_ms": random.random() * 2})  # noqa: S311
    for side in sides:
        side.answer()

    async def read() -> dict[str, Sequence[Event]]:
        opened = await SqliteStore.open(str(path), tenant_id="acme")
        assert isinstance(opened, Ok)
        store = opened.value
        try:
            out: dict[str, Sequence[Event]] = {}
            for label, log in world_logs(world).items():
                got = await store.read(BranchId(str(log["branch_id"])), 0)
                assert isinstance(got, Ok)
                out[label] = got.value.fold.events
            if random.random() < REPLAYED:  # noqa: S311
                await assert_team_replays(store, TEAM)
            return out
        finally:
            await store.close()

    return asyncio.run(read())


def _job(world: Obj, label: str, op: Obj, now: int) -> Obj:
    branch = world_logs(world)[label]["branch_id"]
    return {"branch": branch, "now": now, "op": {**world, **op}}


def _copy(template: Path, where: Path, n: int) -> Path:
    path = where / f"s{n}.db"
    shutil.copyfile(template, path)
    return path


def _sent(log: Sequence[Event], kind: str) -> list[str]:
    return [
        str(e.data.envelope.monitor_id)
        for e in log
        if isinstance(e, MessageSentEvent) and e.data.envelope.kind == kind
    ]


def test_deadline_race_a_settlement_counts_iff_it_deleted_the_monitor_row_first(
    sides: tuple[_Side, _Side], tmp_path: Path
) -> None:
    world = _vector("idle-fires-settle-and-task-monitors")
    template = _template(world, tmp_path)
    wait_id = "0192b000-0000-7000-8000-0000000000b1:c3"
    seen: set[bool] = set()
    for n in range(SCHEDULES):
        logs = _schedule(
            sides,
            world,
            _copy(template, tmp_path, n),
            (
                _job(world, "lead", {"op": "deadline", "input": {"id": wait_id}}, DUE),
                _job(world, "researcher", {"op": "idle", "input": {}}, DUE),
            ),
        )
        started = next(e for e in logs["lead"] if isinstance(e, WaitStartedEvent))
        monitor = f"{started.branch_id}:{started.event_id}:researcher-1"
        fired = monitor in _sent(logs["researcher"], "member_settled")
        (done,) = [e for e in logs["lead"] if isinstance(e, WaitFinishedEvent)]
        # finished is exactly the members whose firing won the monitor row.
        assert [r.member.name for r in done.data.finished] == (["researcher-1"] if fired else [])
        assert done.data.timed_out is not fired
        seen.add(fired)
    assert seen == {True, False}


def test_already_settled_race_exactly_one_of_observation_or_mail(
    sides: tuple[_Side, _Side], tmp_path: Path
) -> None:
    world = _vector("wait-registers-monitor")
    template = _template(world, tmp_path)
    ended: Obj = {
        "reason": "error",
        "result": {"status": "failed", "error": {"code": "model_error", "message": "stopped"}},
    }
    now = world["now"]
    assert isinstance(now, int)
    seen: set[str] = set()
    for n in range(SCHEDULES):
        logs = _schedule(
            sides,
            world,
            _copy(template, tmp_path, n),
            (
                _job(world, "lead", {}, now),
                _job(world, "researcher", {"op": "end", "input": ended}, now),
            ),
        )
        started = next(e for e in logs["lead"] if isinstance(e, WaitStartedEvent))
        monitor = f"{started.branch_id}:{started.event_id}:researcher-1"
        observed = any(
            isinstance(e, MemberObservedEvent) and e.data.monitor_id == monitor
            for e in logs["lead"]
        )
        mailed = monitor in _sent(logs["researcher"], "member_ended")
        assert observed is not mailed
        seen.add("observed" if observed else "mailed")
    assert seen == {"observed", "mailed"}


def test_reply_versus_deadline_answered_iff_the_reply_was_sent(
    sides: tuple[_Side, _Side], tmp_path: Path
) -> None:
    world = _vector("reply-sent")
    template = _template(world, tmp_path)
    ask_id = obj(obj(world["input"])["args"])["ask_id"]
    seen: set[str] = set()
    for n in range(SCHEDULES):
        logs = _schedule(
            sides,
            world,
            _copy(template, tmp_path, n),
            (
                # The reply's clock is just before the deadline or at it.
                _job(world, "researcher", {}, DUE - n % 3),
                _job(world, "writer", {"op": "deadline", "input": {"id": ask_id}}, DUE),
            ),
        )
        replied = len(_sent(logs["researcher"], "reply")) == 1
        closed = [e.data.outcome.status for e in logs["writer"] if isinstance(e, _Closed)]
        assert closed == ["answered" if replied else "timed_out"]
        seen.add(closed[0])
    assert seen == {"answered", "timed_out"}


def test_cancel_versus_the_members_end_refused_or_sent_and_refused_by_the_end(
    sides: tuple[_Side, _Side], tmp_path: Path
) -> None:
    world = _vector("cancel-requested")
    template = _template(world, tmp_path)
    ended: Obj = {
        "reason": "error",
        "result": {"status": "failed", "error": {"code": "model_error", "message": "stopped"}},
    }
    now = world["now"]
    assert isinstance(now, int)
    seen: set[str] = set()
    for n in range(SCHEDULES):
        logs = _schedule(
            sides,
            world,
            _copy(template, tmp_path, n),
            (
                _job(world, "lead", {}, now),
                _job(world, "researcher", {"op": "end", "input": ended}, now),
            ),
        )
        cancels = [
            e.data.envelope.mail_id
            for e in logs["lead"]
            if isinstance(e, MessageSentEvent) and e.data.envelope.kind == "cancel"
        ]
        refused = any(
            isinstance(e, ToolResultEvent) and '"code":"member_ended"' in e.data.preview
            for e in logs["lead"]
        )
        bounced = any(
            isinstance(e, MailRefusedEvent) and e.data.mail_id in cancels
            for e in logs["researcher"]
        )
        # The cancel was sent iff the member was not ended yet, and then its end refuses it.
        assert (len(cancels) == 1) is not refused
        assert bounced is not refused
        seen.add("refused" if refused else "sent")
    assert seen == {"refused", "sent"}
