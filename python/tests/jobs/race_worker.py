"""One side of a two-process race (test_team_races.py), as its own process: for each job line on
stdin it opens the shared store file on its own connection and clock, runs one op vector's op
under the writer of its branch after a random spin, and answers one line on stdout. SQLite
serializes the two sides' transactions (BEGIN IMMEDIATE) in either order. The same as
TypeScript's test/team/race-worker.ts."""

import asyncio
import json
import sys
import time

from pydantic import JsonValue
from team.op_run import run_on
from team.vectors import obj

from threads.log import BranchId
from threads.result import Ok
from threads.store import SqliteStore


async def _run(line: str) -> JsonValue:
    job = obj(json.loads(line))
    now = job["now"]
    spin = job["spin_ms"]
    assert isinstance(now, int)
    assert isinstance(spin, float | int)
    opened = await SqliteStore.open(str(job["path"]), tenant_id="acme")
    assert isinstance(opened, Ok), opened
    store = opened.value
    try:
        got = await store.acquire(BranchId(str(job["branch"])), "race", lambda: now)
        assert isinstance(got, Ok), got
        until = time.perf_counter() + spin / 1000
        while time.perf_counter() < until:
            pass  # spin, not sleep: both sides start their transactions as close as they can
        return await run_on(got.value, obj(job["op"]))
    finally:
        await store.close()


def main() -> None:
    for line in sys.stdin:
        if line.strip():
            print(json.dumps({"outcome": asyncio.run(_run(line))}), flush=True)


if __name__ == "__main__":
    main()
