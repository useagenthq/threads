"""A cancelled append never leaves the writer out of step with what was committed."""

import asyncio
import sqlite3
from pathlib import Path

from pydantic import JsonValue

from threads.log import BranchId, ThreadId
from threads.result import Ok
from threads.store import Draft, SqliteStore

THREAD = ThreadId("0192a000-0000-7000-8000-000000000001")
ROOT = BranchId("0192b000-0000-7000-8000-000000000001")
NOW = 1_790_000_000_000
ALICE: dict[str, JsonValue] = {
    "kind": "user",
    "principal": {"issuer": "api", "tenant": "acme", "subject": "alice"},
}
STARTED = Draft(
    "thread_started",
    {
        "agent_name": "demo",
        "config_hash": "0" * 64,
        "instructions": "x",
        "model": {"provider": "scripted", "name": "scripted-1"},
        "model_params": {"max_tokens": 1024},
        "adapter": {"name": "scripted", "version": "1", "settings": {}},
        "tools": [],
    },
)
USER = Draft("user_input", {"source": "api", "text": "hi"}, actor=ALICE)
DONE = Draft("turn_completed", {"reason": "end_turn"})


def test_an_append_cancelled_while_blocked_keeps_the_chain(tmp_path: Path) -> None:
    """Cancelled while BEGIN IMMEDIATE waits on another connection, the append still commits
    once the lock frees. The writer must know, or its next line breaks the chain."""
    path = tmp_path / "threads.db"

    async def main() -> None:
        opened = await SqliteStore.open(path)
        assert isinstance(opened, Ok)
        store = opened.value
        blocker = sqlite3.connect(path, isolation_level=None)
        try:
            assert await store.create(THREAD, ROOT, NOW) == Ok(None)
            writer = await store.acquire(ROOT, "a", lambda: NOW)
            assert isinstance(writer, Ok)
            assert isinstance(await writer.value.append([STARTED]), Ok)
            blocker.execute("BEGIN IMMEDIATE")
            task = asyncio.create_task(writer.value.append([USER]))
            await asyncio.sleep(0.1)
            task.cancel()
            await asyncio.sleep(0.05)
            blocker.execute("COMMIT")
            await asyncio.gather(task, return_exceptions=True)
            await writer.value.append([DONE])
            read = await store.read(ROOT, NOW)
            assert isinstance(read, Ok), read
            assert read.value.state.head.seq in {2, 3}
        finally:
            blocker.close()
            await store.close()

    asyncio.run(main())
