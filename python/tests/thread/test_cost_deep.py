"""Thread.cost(tree=True) walks descendants at any depth (spec/api.json): a chain of 10,000
subagents is walked with an explicit stack, never the call stack (Python's recursion limit is
1,000)."""

import asyncio

from pydantic import JsonValue

from threads import sqlite
from threads.agents.store import now_ms, open_store
from threads.log import BranchId, ThreadId
from threads.result import Err, Ok
from threads.store import Draft
from threads.store.lines import uuid7
from threads.thread.read import read_log
from threads.thread.usage import tree_cost

DEPTH = 10_000
STARTED: dict[str, JsonValue] = {
    "agent_name": "demo",
    "config_hash": "0" * 64,
    "instructions": "You are a helpful agent.",
    "model": {"provider": "scripted", "name": "scripted-1"},
    "model_params": {"max_tokens": 1024},
    "adapter": {"name": "scripted", "version": "1", "settings": {}},
    "tools": [],
}


def _thread(i: int) -> ThreadId:
    return ThreadId(f"0192a000-0000-7000-8000-{i:012x}")


def _branch(i: int) -> BranchId:
    return BranchId(f"0192b000-0000-7000-8000-{i:012x}")


def _spawned(child: ThreadId, event_id: str) -> Draft:
    data: dict[str, JsonValue] = {
        "call_id": f"s-{child}",
        "child_thread_id": child,
        "agent_name": "kid",
        "mode": "foreground",
        "isolation": "none",
    }
    return Draft("agent_spawned", data, event_id=event_id)


def test_a_chain_of_10000_subagents_is_walked_to_the_bottom_without_recursion() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        db = await open_store(store)
        parent: dict[str, JsonValue] | None = None
        for i in range(DEPTH):
            assert await db.create(_thread(i), _branch(i), now_ms()) == Ok(None)
            writer = await db.acquire(_branch(i), "deep-test", now_ms)
            assert isinstance(writer, Ok)
            # The deepest thread spawns the root again: the walk only meets it at the bottom.
            child = _thread(i + 1 if i + 1 < DEPTH else 0)
            spawn_id = uuid7(now_ms())
            started = STARTED if parent is None else {**STARTED, "parent": parent}
            appended = await writer.value.append(
                [Draft("thread_started", started), _spawned(child, spawn_id)]
            )
            assert isinstance(appended, Ok)
            parent = {
                "thread_id": _thread(i),
                "branch_id": _branch(i),
                "event_id": spawn_id,
                "relation": "subagent",
            }
        root = await read_log(store, _branch(0))
        assert isinstance(root, Ok)
        total = await tree_cost(store, _thread(0), root.value)
        assert isinstance(total, Err)
        assert total.error.code == "log_corrupt"
        # The path names every thread down to the bottom one.
        assert total.error.message.endswith(
            f"child {_thread(DEPTH - 1)}: child {_thread(0)}: "
            f"thread {_thread(0)} appears twice in the tree"
        )

    asyncio.run(main())
