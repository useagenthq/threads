"""The preset at run time: what its permissions actually decide, that the .threads guard still
wins, and that a workspace it passes through reaches the sandbox and fails closed when the copy
on disk moves on.

A scripted model and the fake sandbox, so nothing here reaches Docker or a provider. Invariant 4
(no credential reaches the sandbox) is proven against the real daemon in the Docker job, where
`env` and `/proc/1/environ` can actually be read.
"""

import asyncio
from pathlib import Path

import pytest
from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads import (
    Completed,
    ConfigError,
    Parked,
    fake_sandbox,
    open_thread,
    scripted_model,
    sqlite,
)
from threads.agents.agent import Agent
from threads.coding import coding_agent
from threads.log import Event, PermissionDecisionEvent, ToolResultEvent
from threads.result import Ok
from threads.thread.handle import Thread

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
PIPELINE = "cd app && npm test 2>&1 | tail -50"


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def use(name: str, args: JsonValue, call_id: str) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


async def events_of(thread: Thread) -> list[Event]:
    timeline = await thread.timeline()
    assert isinstance(timeline, Ok)
    return [entry.event for entry in timeline.value.entries]


def decisions(events: list[Event]) -> list[tuple[str, str, str]]:
    return [
        (e.data.decision, e.data.source, "" if e.data.rule_id is MISSING else e.data.rule_id)
        for e in events
        if isinstance(e, PermissionDecisionEvent)
    ]


def previews(events: list[Event]) -> list[str]:
    return [
        "" if e.data.preview is MISSING else e.data.preview
        for e in events
        if isinstance(e, ToolResultEvent)
    ]


def test_a_pipeline_bash_and_an_edit_run_without_a_challenge() -> None:
    model = scripted_model(
        {
            "responses": [
                use("bash", {"command": PIPELINE}, "c1"),
                use("write", {"path": "src/new.ts", "content": "const x = 1;\n"}, "c2"),
                use("edit", {"path": "src/new.ts", "old_string": "1", "new_string": "2"}, "c3"),
                text("done"),
            ]
        }
    )
    bot = coding_agent(model=model, sandbox=fake_sandbox({"tools": {"cd": {"output": ""}}}))

    async def main() -> None:
        result = await bot.run("go", store=sqlite(":memory:"))
        assert isinstance(result, Completed)
        # The pipeline is a bash(*) allow; the edits are the accept_edits mode's own column.
        assert decisions(await events_of(result.thread)) == [
            ("allow", "policy", "bash(*)"),
            ("allow", "mode", ""),
            ("allow", "mode", ""),
        ]

    asyncio.run(main())


def test_a_deny_override_still_denies_before_bash_any_is_consulted() -> None:
    model = scripted_model(
        {"responses": [use("bash", {"command": "rm -rf build"}, "c1"), text("stopped")]}
    )
    bot = coding_agent(
        model=model,
        sandbox=fake_sandbox(),
        permissions={"mode": "accept_edits", "allow": ["bash(*)"], "deny": ["bash(rm:*)"]},
    )

    async def main() -> None:
        result = await bot.run("go", store=sqlite(":memory:"))
        assert decisions(await events_of(result.thread)) == [("deny", "policy", "bash(rm:*)")]

    asyncio.run(main())


def test_plan_mode_denies_bash_and_write_and_allows_todo_write() -> None:
    todos: JsonValue = {"todos": [{"id": "1", "content": "look around", "status": "pending"}]}
    model = scripted_model(
        {
            "responses": [
                use("bash", {"command": "npm test"}, "c1"),
                use("write", {"path": "src/new.ts", "content": "x"}, "c2"),
                use("todo_write", todos, "c3"),
                text("read-only"),
            ]
        }
    )
    bot = coding_agent(model=model, sandbox=fake_sandbox(), permissions={"mode": "plan"})

    async def main() -> None:
        result = await bot.run("go", store=sqlite(":memory:"))
        assert decisions(await events_of(result.thread)) == [
            ("deny", "mode", ""),
            ("deny", "mode", ""),
            ("allow", "mode", ""),
        ]

    asyncio.run(main())


def test_a_memory_write_parks_and_the_challenge_names_the_option_to_change() -> None:
    model = scripted_model(
        {"responses": [use("save_memory", {"text": "the tests run with pytest"}, "c1")]}
    )
    bot = coding_agent(model=model, sandbox=fake_sandbox())

    async def main() -> None:
        store = sqlite(":memory:")
        result = await bot.run("remember", store=store)
        assert isinstance(result, Parked)
        pending = await result.thread.pending_approvals()
        assert isinstance(pending, Ok)
        assert pending.value[0].reason == (
            'memory_write is "ask": approve this call, or set memory_write to '
            '"allow_principal" or "allow"'
        )

    asyncio.run(main())


def test_bash_any_is_consulted_after_the_threads_guard_which_denies_both_ways_in() -> None:
    model = scripted_model(
        {
            "responses": [
                use("bash", {"command": "echo x > .threads/agents.py"}, "c1"),
                use("write", {"path": ".threads/x", "content": "x"}, "c2"),
                text("refused"),
            ]
        }
    )
    bot = coding_agent(model=model, sandbox=fake_sandbox())

    async def main() -> None:
        result = await bot.run("go", store=sqlite(":memory:"))
        assert decisions(await events_of(result.thread)) == [
            ("deny", "self_config_guard", ""),
            ("deny", "self_config_guard", ""),
        ]

    asyncio.run(main())


def _reader(root: Path) -> Agent[None, str]:
    model = scripted_model(
        {"responses": [use("read", {"path": "src/cli.py"}, "c1"), text("read it")]}
    )
    return coding_agent(model=model, sandbox=fake_sandbox(), workspace={"local_dir": str(root)})


def test_the_sandbox_starts_with_the_copied_files_and_a_changed_copy_fails_closed(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "cli.py").write_text("FLAGS = []\n")

    async def main() -> None:
        store = sqlite(":memory:")
        result = await _reader(tmp_path).run("what does the CLI do?", store=store)
        assert isinstance(result, Completed)
        assert "FLAGS = []" in previews(await events_of(result.thread))[0]

        # The copy is pinned as bytes, so continuing the thread after ./app changed fails closed.
        (tmp_path / "src" / "cli.py").write_text("FLAGS = ['--json']\n")
        opened = await open_thread(store, result.thread.id)
        assert isinstance(opened, Ok)
        with pytest.raises(ConfigError) as refused:
            await _reader(tmp_path).run("and now?", store=store, thread=result.thread)
        assert refused.value.code == "invalid_config"
        assert refused.value.message == (
            "the workspace changed since this thread started; start a new thread"
        )

    asyncio.run(main())
