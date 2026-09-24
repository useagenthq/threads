"""A store process for the team drills (test_team.py), run as a real child process on one
file-backed store. `python team_worker.py <role> <dir>`:

- `lead`: opens a lead's log with its first append (thread_started{team}, user_input), which
  opens the team log, the teams row and the lead's row in the same transaction. With
  `DRILL_STOP_AT=lead_first_append` it prints `at lead_first_append` and blocks mid-transaction,
  at the first feed row, until the parent kills it.
- `open-a`, `open-b`: once `<dir>/go` exists, runs branch.open on one shared branch, each with
  its own thread id and holder, and prints `opened` or `already_open`.
"""

import asyncio
import sqlite3
import sys
import time
from pathlib import Path
from typing import Final

from jobs.worker import reached
from pydantic import JsonValue

from threads.log import BranchId, ThreadId
from threads.result import Ok
from threads.store import Draft, SqliteStore

TENANT: Final = "acme"
TEAM: Final = "0192c000-0000-7000-8000-000000000001"
LEAD: Final = ThreadId("0192a000-0000-7000-8000-0000000000b1")
LEAD_BRANCH: Final = BranchId("0192b000-0000-7000-8000-0000000000b1")
TEAM_LOG: Final = BranchId("0192b000-0000-7000-8000-0000000000b3")
RACED: Final = BranchId("0192b000-0000-7000-8000-0000000000c1")
"""The branch both open-* workers race to open, each with its own thread."""
RACER_THREAD: Final = {
    "open-a": ThreadId("0192a000-0000-7000-8000-0000000000ca"),
    "open-b": ThreadId("0192a000-0000-7000-8000-0000000000cb"),
}


def _started(team: JsonValue = None) -> Draft:
    data: dict[str, JsonValue] = {
        "agent_name": "lead",
        "config_hash": "a" * 64,
        "model": {"provider": "scripted", "name": "scripted-1"},
        "model_params": {"max_tokens": 1024},
        "adapter": {"name": "scripted", "version": "1", "settings": {}},
        "instructions": "You lead.",
        "tools": [],
    }
    return Draft("thread_started", data if team is None else {**data, "team": team})


def _input(text: str) -> Draft:
    principal: JsonValue = {"issuer": "api", "tenant": TENANT, "subject": "alice"}
    return Draft(
        "user_input", {"source": "api", "text": text}, {"kind": "user", "principal": principal}
    )


async def _store(where: Path) -> SqliteStore:
    opened = await SqliteStore.open(where / "threads.db", tenant_id=TENANT)
    assert isinstance(opened, Ok)
    return opened.value


async def lead(where: Path) -> str:
    store = await _store(where)

    def stop_at_the_feed(conn: sqlite3.Connection) -> None:
        conn.set_trace_callback(
            lambda sql: (
                reached("lead_first_append", where)
                if sql.startswith("INSERT INTO team_feed")
                else None
            )
        )

    await store.run(stop_at_the_feed)
    team: JsonValue = {"id": TEAM, "log_branch_id": TEAM_LOG}
    drafts = [_started(team), _input("Lead the team.")]
    opened = await store.open_branch(LEAD, LEAD_BRANCH, drafts, holder_id="lead", clock=_now)
    assert isinstance(opened, Ok), opened
    return "opened"


async def race(role: str, where: Path) -> str:
    while not (where / "go").exists():
        time.sleep(0.001)
    store = await _store(where)
    drafts = [_started(), _input(role)]
    opened = await store.open_branch(RACER_THREAD[role], RACED, drafts, holder_id=role, clock=_now)
    assert isinstance(opened, Ok), opened
    return "already_open" if opened.value == "already_open" else "opened"


def _now() -> int:
    return time.time_ns() // 1_000_000


if __name__ == "__main__":
    role, where = sys.argv[1], Path(sys.argv[2])
    said = asyncio.run(lead(where) if role == "lead" else race(role, where))
    print(said, flush=True)
