"""Children across process restarts and sandboxes (F7.4, F7.9): a background child a crash left
running is restarted once and finishes once; a shared_sandbox child runs in the parent's
sandbox; a team message is delivered into a member once, however often delivery runs."""

import asyncio
from pathlib import Path

import pytest
from local_sandbox import LocalSandbox
from pydantic import JsonValue

from threads import Completed, EventItem, Thread, agent, scripted_model, sqlite
from threads.log import (
    AgentFinishedEvent,
    AgentSpawnedEvent,
    Event,
    InjectedEvent,
    Permissions,
    ToolResultEvent,
    ToolResultLateEvent,
)
from threads.loop.scripted import ScriptExhaustedError
from threads.result import Ok
from threads.thread.handle import open_thread

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
BYPASS = Permissions(
    mode="bypass",
    allow=[],
    ask=[],
    deny=[],
    protected_paths=[],
    allow_bypass=True,
    plan_exit_mode="default",
)


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def call(name: str, args: JsonValue, call_id: str = "call_1") -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


async def events_of(thread: Thread) -> list[Event]:
    timeline = await thread.timeline()
    assert isinstance(timeline, Ok)
    return [e.event for e in timeline.value.entries]


def test_a_background_child_left_running_by_a_crash_finishes_exactly_once() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        spawn = call("spawn_agent", {"agent": "scanner", "prompt": "Scan.", "background": True})
        broken = agent(name="scanner", model=scripted_model({"responses": []}))
        lead = agent(model=scripted_model({"responses": [spawn, text("ok")]}), subagents=[broken])
        stream = lead.stream("go", store=store)
        seen = [item.event async for item in stream if isinstance(item, EventItem)]
        with pytest.raises(ScriptExhaustedError):
            await stream.result
        parent = Thread(seen[0].thread_id, seen[0].branch_id, store)
        fixed = agent(name="scanner", model=scripted_model({"responses": [text("clean")]}))
        again = agent(
            model=scripted_model({"responses": [text("next")]}),
            subagents=[fixed],
        )
        result = await again.run("status?", store=store, thread=parent)
        assert isinstance(result, Completed)
        events = await events_of(result.thread)
        assert len([e for e in events if isinstance(e, AgentSpawnedEvent)]) == 1
        finished = [e for e in events if isinstance(e, AgentFinishedEvent)]
        assert [f.data.status for f in finished] == ["completed"]
        late = [e for e in events if isinstance(e, ToolResultLateEvent)]
        assert [r.data.preview for r in late] == ["clean"]

    asyncio.run(main())


def test_a_shared_sandbox_child_runs_in_the_parents_sandbox(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    box = LocalSandbox(tmp_path / "ws")
    write = call("write", {"path": "note.txt", "content": "from the child"})

    async def main() -> None:
        child = agent(
            name="writer",
            model=scripted_model({"responses": [write, text("wrote it")]}),
            sandbox=box,
            permissions=BYPASS,
        )
        lead = agent(
            model=scripted_model(
                {
                    "responses": [
                        call("spawn_agent", {"agent": "writer", "prompt": "Write a note."}),
                        call("read", {"path": "note.txt"}, "call_2"),
                        text("done"),
                    ]
                }
            ),
            sandbox=box,
            permissions=BYPASS,
            subagents=[child],
        )
        store = sqlite(str(tmp_path / "db" / "threads.db"))
        result = await lead.run("go", store=store)
        assert isinstance(result, Completed)
        events = await events_of(result.thread)
        spawned = next(e for e in events if isinstance(e, AgentSpawnedEvent))
        assert spawned.data.isolation == "shared_sandbox"
        read = [e for e in events if isinstance(e, ToolResultEvent)][-1]
        assert "from the child" in read.data.preview

    asyncio.run(main())


def test_a_team_message_is_delivered_into_a_member_once() -> None:
    todo: JsonValue = {"todos": [{"id": "1", "content": "t1", "status": "pending"}]}
    alice_script: list[JsonValue] = [call("todo_write", todo), text("on it")]

    async def main() -> None:
        alice = agent(
            name="alice",
            model=scripted_model({"responses": alice_script}),
        )
        lead = agent(
            model=scripted_model(
                {
                    "responses": [
                        call("send_message", {"to": "alice", "text": "take t1"}),
                        call("spawn_agent", {"agent": "alice", "prompt": "Work."}, "call_2"),
                        text("done"),
                    ]
                }
            ),
            subagents=[alice],
        )
        store = sqlite(":memory:")
        result = await lead.run("go", store=store)
        assert isinstance(result, Completed)
        spawned = next(
            e for e in await events_of(result.thread) if isinstance(e, AgentSpawnedEvent)
        )
        child = await open_thread(store, spawned.data.child_thread_id)
        assert isinstance(child, Ok)
        inside = await events_of(child.value)
        notes = [e for e in inside if isinstance(e, InjectedEvent) and e.data.source == "agent"]
        # Delivery ran before both of alice's requests; the message went in once.
        assert [n.data.text for n in notes] == ["agent: take t1"]
        requests = [e for e in inside if e.type == "model_request"]
        assert len(requests) == len(alice_script)

    asyncio.run(main())
