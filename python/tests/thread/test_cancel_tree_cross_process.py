"""A tree cancel whose target runs in another process (lane 29F). Both the parent's and the
child's leases are held there, so neither barrier can be appended: each becomes a durable control
item, and the holder applies them at its own boundaries. The order the design names still holds:
the parent's runner records its child's agent_finished{cancelled} before its own cancelled,
because the child's item is applied first and the parent's end waits for it.
"""

import asyncio
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from control_item_kit import rows
from team.crash_kit import OPERATOR

from threads._generated.host_api_v1 import CancelAccepted
from threads.agents.store import Store, open_store, sqlite
from threads.log import AgentFinishedEvent, AgentSpawnedEvent, Event, ThreadId
from threads.result import Ok
from threads.thread.handle import Thread, open_thread

WORKER = Path(__file__).with_name("cancel_tree_worker.py")
TREE = 2
"""The barriers a tree cancel of this shape needs: the parent's and its one child's."""


@contextmanager
def holder(path: Path) -> Generator[subprocess.Popen[str]]:
    """The other process: it holds the whole tree's leases until its tool is released."""
    proc = subprocess.Popen(  # noqa: S603 - a test worker in this repo
        [sys.executable, str(WORKER), str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    with proc:
        try:
            yield proc
        finally:
            proc.kill()


def line(proc: subprocess.Popen[str]) -> str:
    assert proc.stdout is not None
    got = proc.stdout.readline()
    assert got, "the holder exited"
    return got.strip()


def go(proc: subprocess.Popen[str]) -> None:
    assert proc.stdin is not None
    proc.stdin.write("go\n")
    proc.stdin.flush()


async def _events(store: Store, thread: ThreadId) -> list[Event]:
    opened = await open_thread(store, thread)
    assert isinstance(opened, Ok), opened
    timeline = await opened.value.timeline()
    assert isinstance(timeline, Ok), timeline
    return [entry.event for entry in timeline.value.entries]


async def _parent(store: Store) -> ThreadId:
    """The thread whose log spawned the other one."""
    sq = await open_store(store)
    found = await sq.run(lambda c: c.execute("SELECT DISTINCT thread_id FROM branches").fetchall())
    for (thread,) in found:
        at = ThreadId(str(thread))
        if any(isinstance(e, AgentSpawnedEvent) for e in await _events(store, at)):
            return at
    raise AssertionError("no parent thread yet")


def test_a_tree_cancel_of_a_holder_elsewhere_ends_the_child_before_the_parent(
    tmp_path: Path,
) -> None:
    where = tmp_path / "s"
    db = where / "threads.db"

    async def cancel() -> CancelAccepted | None:
        store = sqlite(str(where))
        parent = await _parent(store)
        branch = await (await open_store(store)).root(parent)
        assert isinstance(branch, Ok)
        done = await Thread(parent, branch.value, store).cancel(OPERATOR)
        assert isinstance(done, Ok), done
        return done.value if isinstance(done.value, CancelAccepted) else None

    async def read() -> tuple[list[str], list[str]]:
        store = sqlite(str(where))
        parent = await _parent(store)
        events = await _events(store, parent)
        finished = next(e for e in events if isinstance(e, AgentFinishedEvent))
        assert finished.data.status == "cancelled"
        spawned = next(e for e in events if isinstance(e, AgentSpawnedEvent))
        child = await _events(store, spawned.data.child_thread_id)
        return [e.type for e in events], [e.type for e in child]

    with holder(db.parent) as other:
        assert line(other) == "running"
        # Both leases are the other process's, so both barriers are durable items, not appends.
        accepted = asyncio.run(cancel())
        assert accepted is not None
        assert len(rows(db)) == TREE
        go(other)
        assert line(other) == "done cancelled"

    parent_log, child_log = asyncio.run(read())
    # The order of the design: the child's end is on record before the parent's own cancelled.
    assert parent_log.index("agent_finished") < parent_log.index("cancelled")
    barrier = child_log.index("cancel_requested")
    later = child_log[barrier:]
    assert "model_request" not in later
    assert "cancelled" in later
    # Each item was applied exactly once, in the append that recorded its barrier.
    applied = rows(db)
    assert len(applied) == TREE
    assert all(row.consumed_seq is not None for row in applied)
