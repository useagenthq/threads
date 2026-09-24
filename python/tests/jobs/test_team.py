"""Team store drills on real processes: a lead killed with SIGKILL inside its first append leaves
nothing of it (all-or-nothing), and two processes racing branch.open on one branch leave one
opener, the other told already_open, and no orphan."""

import contextlib
import sqlite3
from pathlib import Path

import pytest
from jobs.drill import WAIT_S, kill, spawn, wait_at
from jobs.team_worker import RACED, RACER_THREAD, TEAM, TEAM_LOG

pytestmark = pytest.mark.jobs

WORKER = Path(__file__).with_name("team_worker.py")
TESTS = str(WORKER.parent.parent)


def _query(where: Path, sql: str, *params: str) -> list[tuple[object, ...]]:
    with contextlib.closing(sqlite3.connect(where / "threads.db")) as db:
        return db.execute(sql, params).fetchall()


def _said(where: Path, role: str) -> str:
    worker = spawn(role, where, WORKER, PYTHONPATH=TESTS)
    out, _ = worker.communicate(timeout=WAIT_S)
    assert worker.returncode == 0
    return out.strip()


def test_a_lead_killed_inside_its_first_append_leaves_nothing(tmp_path: Path) -> None:
    first = spawn("lead", tmp_path, WORKER, PYTHONPATH=TESTS, DRILL_STOP_AT="lead_first_append")
    wait_at(first, "lead_first_append")
    kill(first)
    for table in ("branches", "events", "leases", "teams", "team_members", "team_feed"):
        assert _query(tmp_path, f"SELECT * FROM {table}") == []  # noqa: S608

    assert _said(tmp_path, "lead") == "opened"
    assert _query(tmp_path, "SELECT team_id, team_log_branch_id FROM teams") == [(TEAM, TEAM_LOG)]
    assert _query(tmp_path, "SELECT name, role FROM team_members") == [("lead", "lead")]
    events = _query(tmp_path, "SELECT type FROM events WHERE branch_id = ?", TEAM_LOG)
    assert events == [("team_opened",)]


def test_two_processes_racing_branch_open_leave_one_opener_and_no_orphan(tmp_path: Path) -> None:
    racers = {
        role: spawn(role, tmp_path, WORKER, PYTHONPATH=TESTS) for role in ("open-a", "open-b")
    }
    (tmp_path / "go").touch()
    said: dict[str, str] = {}
    for role, worker in racers.items():
        out, _ = worker.communicate(timeout=WAIT_S)
        assert worker.returncode == 0
        said[role] = out.strip()

    assert sorted(said.values()) == ["already_open", "opened"]
    winner = next(role for role, s in said.items() if s == "opened")
    assert _query(tmp_path, "SELECT thread_id FROM threads") == [(RACER_THREAD[winner],)]
    leases = _query(tmp_path, "SELECT branch_id, holder_id, epoch FROM leases")
    assert leases == [(RACED, winner, 1)]
    seqs = _query(tmp_path, "SELECT seq, type FROM events ORDER BY seq")
    assert seqs == [(1, "thread_started"), (2, "user_input")]
