"""The other process of the host team's lazy-open race (test_host_drills.py): it opens the same
tenant's host team on the same store file. Both processes derive the same ids, so one wins the
branch's primary key and the other gets `already_open`. It prints `opened` and exits."""

import asyncio
import sys

from threads import sqlite
from threads.agents.store import now_ms, open_store, scoped
from threads.result import Ok
from threads.team.host_open import ensure_host_team

TENANT = "acme"
CONFIG = "5800e46921bd898ffefe26cbb45e8038fd946b9719bd1f0e155c1d94aa9f459b"


async def main(path: str, go: str) -> None:
    sq = await open_store(scoped(sqlite(path), TENANT))
    print("ready", flush=True)
    # Both processes wait for the same line, so their opens overlap.
    assert sys.stdin.readline().strip() == go
    got = await ensure_host_team(sq, TENANT, {"billing": CONFIG}, holder="other", clock=now_ms)
    print("opened" if isinstance(got, Ok) else "failed", flush=True)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2]))
