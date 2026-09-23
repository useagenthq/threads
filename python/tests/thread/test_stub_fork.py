"""A fork run in stub mode (spec/api.json Thread.fork `mode`): every
mediated operation is answered from what the parent branch recorded after the fork point, by
(tool, args_hash, occurrence); an unmatched one fails closed and never goes live. A live model
with hosted tools is refused before recovery or any dispatch, since hosted calls can't be
stubbed. Stub mode needs a sandbox that enforces deny-all egress."""

import asyncio
from dataclasses import replace

import pytest
from pydantic import BaseModel, JsonValue

from threads import (
    Agent,
    Completed,
    Failed,
    RunContext,
    Thread,
    agent,
    fake_sandbox,
    scripted_model,
    sqlite,
    tool,
)
from threads.agents.config import ConfigError
from threads.anthropic import anthropic
from threads.log import Permissions, ToolResultEvent
from threads.result import Err, Ok
from threads.sandbox.fake import FakeSandbox
from threads.sandbox.protocol import SandboxInfo

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


class OpenEgress(FakeSandbox):
    """The fake provider, declaring egress it can't enforce."""

    @property
    def info(self) -> SandboxInfo:
        return replace(super().info, egress="unenforced")


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def use(name: str, args: JsonValue, call_id: str = "call_1") -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


class Note(BaseModel):
    text: str


def bot(
    box: FakeSandbox,
    sent: list[str],
    responses: list[JsonValue],
    worker: list[JsonValue] | None = None,
) -> Agent[None, str]:
    """One agent config for every run of the thread (a changed config starts a new thread),
    with a worker subagent that can send too."""

    async def send(args: Note, _ctx: RunContext[None]) -> str:
        sent.append(args.text)
        return f"sent {args.text}"

    send_tool = tool(name="send", description="Send.", input=Note, runs="host", execute=send)
    helper = agent(
        name="worker",
        model=scripted_model({"responses": worker or []}),
        tools=[send_tool],
        permissions=BYPASS,
    )
    script: JsonValue = {"responses": responses}
    return agent(
        model=scripted_model(script),
        sandbox=box,
        tools=[send_tool],
        permissions=BYPASS,
        subagents=[helper],
    )


async def recorded(box: FakeSandbox, sent: list[str]) -> Thread:
    """A thread with a fork point after its first turn, then a live send in its second."""
    store = sqlite(":memory:")
    write = use("write", {"path": "a.txt", "content": "A"}, "w1")
    first = await bot(box, sent, [write, text("Wrote it.")]).run("write", store=store, deps=None)
    assert isinstance(first, Completed)
    second = [use("send", {"text": "x"}), text("Sent.")]
    again = await bot(box, sent, second).run("send", store=store, deps=None, thread=first.thread)
    assert isinstance(again, Completed)
    return first.thread


async def stub_child(thread: Thread) -> Thread:
    points = await thread.fork_points()
    assert isinstance(points, Ok)
    child = await thread.fork(points.value[0], mode="stub")
    assert isinstance(child, Ok), child
    return child.value


def test_a_stub_fork_answers_mediated_calls_from_the_recorded_branch() -> None:
    sent: list[str] = []

    async def main() -> None:
        box = fake_sandbox()
        thread = await recorded(box, sent)
        assert sent == ["x"]
        child = await stub_child(thread)
        replay = [use("send", {"text": "x"}), text("Sent again.")]
        done = await bot(box, sent, replay).run("send", deps=None, thread=child)
        assert isinstance(done, Completed), done
        timeline = await done.thread.timeline()
        assert isinstance(timeline, Ok)
        results = [e.event for e in timeline.value.entries if isinstance(e.event, ToolResultEvent)]
        assert results[-1].data.preview == "sent x"
        # A call the branch never recorded fails closed: settled not_sent, never live.
        other = await stub_child(thread)
        changed = [use("send", {"text": "y"}), text("never")]
        refused = await bot(box, sent, changed).run("send", deps=None, thread=other)
        assert isinstance(refused, Failed), refused
        assert refused.error.code == "unmatched_external_op"

    asyncio.run(main())
    assert sent == ["x"]


def test_a_subagent_of_a_stub_run_is_in_stub_mode_too() -> None:
    sent: list[str] = []

    async def main() -> None:
        box = fake_sandbox()
        thread = await recorded(box, sent)
        child = await stub_child(thread)
        spawn = use("spawn_agent", {"agent": "worker", "prompt": "Send y."}, "sp")
        worker = [use("send", {"text": "y"}), text("never")]
        await bot(box, sent, [spawn, text("ok")], worker).run("delegate", deps=None, thread=child)

    asyncio.run(main())
    # The child's send was never recorded: it fails closed instead of going live.
    assert sent == ["x"]


def test_a_stub_run_refuses_a_live_model_with_hosted_tools() -> None:
    async def main() -> None:
        box = fake_sandbox()
        thread = await recorded(box, [])
        child = await stub_child(thread)
        search: JsonValue = {"type": "web_search_20250305", "name": "web_search"}
        live = anthropic(
            "claude-test",
            hosted_tools=[search],
            context_window=1,
            max_output_tokens=1,
            api_key="sk-test-1",
        )
        hosted = bot(box, [], [])
        hosted = Agent(replace(hosted.definition, model=live), (None,), str)
        with pytest.raises(ConfigError) as raised:
            await hosted.run("go", deps=None, thread=child)
        assert raised.value.code == "hosted_tool_unsupported"

    asyncio.run(main())


def test_stub_mode_needs_a_sandbox_that_enforces_egress() -> None:
    async def main() -> None:
        box = fake_sandbox()
        thread = await recorded(box, [])
        points = await thread.fork_points()
        assert isinstance(points, Ok)
        handle = Thread(thread.id, thread.branch, thread.store, sandbox=OpenEgress(box.script))
        refused = await handle.fork(points.value[0], mode="stub")
        assert isinstance(refused, Err)
        assert refused.error.code == "egress_policy_unsupported"

    asyncio.run(main())
