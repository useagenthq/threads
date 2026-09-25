"""A lead running its team, as a real child process on one file-backed store, for the team run
drills (test_team.py). `python team_run.py <role> <dir>`:

- `run`: a lead starts a researcher, answers, and wakes for its result. With
  `DRILL_STOP_AT=member_settle` it prints `at member_settle` and blocks inside the researcher's
  settling append (its member_idle), until the parent kills it.
- `resume`: the restart: the host's recovery of the open run, no new input. Prints the run's
  status.
"""

import asyncio
import sys
from pathlib import Path

from jobs.stores import OnStatement, drill_store
from jobs.worker import reached
from pydantic import JsonValue

from threads import Agent, Principal, agent, scripted_model
from threads.agents.run import RunOptions, execute
from threads.agents.store import open_store
from threads.log import ThreadId
from threads.result import Ok
from threads.store import sqlite_driver
from threads.store.conn import Params, SqliteConn
from threads.store.sql import text_of
from threads.thread.handle import Thread

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
START: JsonValue = {
    "content": [
        {
            "type": "tool_use",
            "call_id": "c1",
            "name": "start",
            "input": {"agent": "researcher", "task": "Research."},
        }
    ],
    "stop_reason": "tool_use",
    "usage": USAGE,
}
FOUND = "Found it."


def _say(text: str) -> JsonValue:
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "usage": USAGE}


def _lead(*, fresh: bool) -> Agent[None, str]:
    """The lead and its team; a fresh process starts the script over."""
    opening = [START] if fresh else []
    researcher = agent(
        name="researcher", model=scripted_model({"responses": [_say(FOUND), _say(FOUND)]})
    )
    script = [*opening, _say("Started."), _say("Final."), _say("Final.")]
    return agent(name="lead", model=scripted_model({"responses": script}), team=[researcher])


def _settling(where: Path) -> OnStatement:
    """Blocks inside the researcher's settling append: the member_idle whose result carries its
    answer."""
    answer = FOUND.encode()

    def at(sql: str, params: Params) -> None:
        if sql.startswith("UPDATE team_members SET result") and any(
            isinstance(p, bytes) and answer in p for p in params
        ):
            reached("member_settle", where)

    return at


def _blocking(where: Path) -> None:
    """On SQLite, every connection this process opens traces its statements the same way."""
    connect, at, hexed = sqlite_driver.connect, _settling(where), FOUND.encode().hex().upper()

    def trace(statement: str) -> None:
        if statement.startswith("UPDATE team_members SET result") and hexed in statement.upper():
            at(statement, (FOUND.encode(),))

    def opened(path: str) -> SqliteConn:
        conn = connect(path)
        conn.raw.set_trace_callback(trace)
        return conn

    sqlite_driver.connect = opened


async def run(where: Path) -> str:
    _blocking(where)
    store = drill_store(where, _settling(where))
    r = await _lead(fresh=True).run("Work.", store=store)
    return r.status


async def resume(where: Path) -> str:
    store = drill_store(where)
    sq = await open_store(store)
    rows = await sq.run(lambda c: c.execute("SELECT lead_thread_id FROM teams").fetchall())
    lead = ThreadId(text_of(rows[0][0]))
    branch = await sq.root(lead)
    assert isinstance(branch, Ok), branch
    principal = Principal(issuer="api", tenant="local", subject="operator")
    options: RunOptions[None] = {
        "store": store,
        "principal": principal,
        "thread": Thread(lead, branch.value, store),
    }
    result = await execute(_lead(fresh=False).definition, None, options, None, lambda _e: None)
    return result.status


if __name__ == "__main__":
    role, where = sys.argv[1], Path(sys.argv[2])
    said = asyncio.run(run(where) if role == "run" else resume(where))
    print(said, flush=True)
