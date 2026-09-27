"""The Teams Phase 2 lazy-open drills (lane 29D): a host team's ids are derived, so opening it
is idempotent and racing it is decided by the branch's primary key.

1. A crash before the commit leaves no host team; after the commit there is one, and the second
   open is `already_open` and writes nothing.
2. Two real processes on one store file open it at once: one `team_opened` and one
   `member_started` per configured member, and the loser's open is `already_open`.

The same drills as TypeScript's test/team/host-open-drills.test.ts.
"""

import asyncio
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from team.crash_kit import CrashError, Point, crashing, reached
from team.team_kit import assert_team_replays

from threadsai import sqlite
from threadsai.agents.store import now_ms, open_store, scoped
from threadsai.log import MemberStartedEvent, TeamOpenedEvent
from threadsai.result import Err, Ok
from threadsai.store import SqliteStore
from threadsai.store.conn import Conn
from threadsai.store.sql import int_of
from threadsai.team.host_open import ensure_host_team
from threadsai.team.host_team import host_team_ids

TENANT = "acme"
IDS = host_team_ids(TENANT)
CONFIG = "5800e46921bd898ffefe26cbb45e8038fd946b9719bd1f0e155c1d94aa9f459b"
MEMBERS = {"billing": CONFIG}


async def _opened(sq: SqliteStore) -> list[str]:
    """The host team log's event types."""
    read = await sq.read(IDS.log_branch_id, now_ms())
    return [] if isinstance(read, Err) else [e.type for e in read.value.fold.events]


def _teams(conn: Conn) -> int:
    row = conn.execute("SELECT COUNT(*) FROM teams").fetchone()
    return 0 if row is None else int_of(row[0])


def test_a_crash_before_the_host_teams_commit_leaves_no_host_team(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    at = Point("the teams row", lambda _c, sql, _p: sql.startswith("INSERT INTO teams"))

    async def main() -> None:
        store = await crashing(tmp_path / "t.db", monkeypatch, at)
        sq = await open_store(store)
        with pytest.raises(CrashError):
            await ensure_host_team(sq, TENANT, MEMBERS, holder="one", clock=now_ms)
        assert reached(), "the drill reached its commit point"
        # A fresh process: the whole open rolled back, so nothing of the team is stored.
        again = await open_store(scoped(sqlite(str(tmp_path / "t.db")), TENANT))
        assert await again.run(_teams) == 0
        assert await _opened(again) == []

    asyncio.run(main())


def test_after_the_commit_the_second_open_is_already_open_and_writes_nothing(
    tmp_path: Path,
) -> None:
    async def main() -> None:
        sq = await open_store(scoped(sqlite(str(tmp_path / "t.db")), TENANT))
        first = await ensure_host_team(sq, TENANT, MEMBERS, holder="one", clock=now_ms)
        assert isinstance(first, Ok), first
        assert await _opened(sq) == ["team_opened", "member_started"]
        # A second process at the same derived ids: the branch exists, so its open is a no-op.
        second = await ensure_host_team(sq, TENANT, MEMBERS, holder="two", clock=now_ms)
        assert isinstance(second, Ok), second
        assert await _opened(sq) == ["team_opened", "member_started"]
        assert await sq.run(_teams) == 1
        await assert_team_replays(sq, IDS.team)

    asyncio.run(main())


WORKER = Path(__file__).with_name("host_open_worker.py")
GO = "go"


@contextmanager
def _other_process(path: Path) -> Generator[subprocess.Popen[str]]:
    """A real second process on the same store file: the race is decided by the primary key on
    the derived branch, which only two connections can contend for."""
    proc = subprocess.Popen(  # noqa: S603 - a test worker in this repo
        [sys.executable, str(WORKER), str(path), GO],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    with proc:
        try:
            yield proc
        finally:
            proc.kill()


def _line(proc: subprocess.Popen[str]) -> str:
    assert proc.stdout is not None
    got = proc.stdout.readline()
    assert got, "the other process exited"
    return got.strip()


def test_two_processes_racing_the_open_leave_one_team_and_one_start_per_member(
    tmp_path: Path,
) -> None:
    """Both opens carry the same derived ids, so the loser collides on the branch's primary
    key and gets already_open: never two team_opened, never two member_started."""
    path = tmp_path / "t.db"

    async def main(proc: subprocess.Popen[str]) -> None:
        sq = await open_store(scoped(sqlite(str(path)), TENANT))
        assert _line(proc) == "ready"
        assert proc.stdin is not None
        proc.stdin.write(f"{GO}\n")
        proc.stdin.flush()
        mine = await ensure_host_team(sq, TENANT, MEMBERS, holder="one", clock=now_ms)
        assert isinstance(mine, Ok), mine
        assert _line(proc) == "opened"
        read = await sq.read(IDS.log_branch_id, now_ms())
        assert isinstance(read, Ok), read
        events = read.value.fold.events
        assert sum(isinstance(e, TeamOpenedEvent) for e in events) == 1
        assert [e.data.member.name for e in events if isinstance(e, MemberStartedEvent)] == [
            "billing"
        ]
        await assert_team_replays(sq, IDS.team)

    with _other_process(path) as proc:
        asyncio.run(main(proc))
