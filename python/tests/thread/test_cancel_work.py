"""No new work after a cancel barrier: a cancel that lands while a call is authorized, or while a
handoff is prepared, closes the call not_executed and ends the turn cancelled. Nothing is
dispatched, and no handoff target starts."""

import asyncio
from collections.abc import Sequence

import pytest
from cancel_kit import Cancel, ended_cancelled
from compact_kit import logged
from hook_kit import ALLOW, Box, Echo, text, use
from pydantic import JsonValue

from threads import HandedOff, RunContext, agent, scripted_model, sqlite, tool
from threads.agents import handoff
from threads.hooks.extension import extension
from threads.hooks.types import SwitchGate, ToolGate
from threads.log import Event, HandoffEvent, ToolCallData, ToolResultEvent
from threads.loop.runtime import Runtime

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}


def _handoff_call() -> JsonValue:
    part: JsonValue = {
        "type": "tool_use",
        "call_id": "c1",
        "name": "handoff",
        "input": {"agent": "billing"},
    }
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


def _nothing_after_the_barrier(events: Sequence[Event], opened: str) -> None:
    names = [e.type for e in events]
    rest = names[names.index("cancel_requested") :]
    assert opened not in rest, rest
    closed = [e for e in events if isinstance(e, ToolResultEvent)]
    assert closed[-1].data.origin == "not_executed"
    ended_cancelled(events)


def test_a_cancel_while_a_call_is_authorized_dispatches_nothing() -> None:
    cancel = Cancel()

    async def gate(_call: ToolCallData, _ctx: RunContext[None]) -> ToolGate:
        await cancel()
        return {"decision": "allow"}

    box = Box()
    echo = tool(name="echo", description="Echo.", input=Echo, runs="host", execute=box.run)
    bot = agent(
        model=scripted_model({"responses": [text("hi"), use(), text("done")]}),
        tools=[echo],
        permissions=ALLOW,
        extensions=[extension(name="ops", hooks={"before_tool": gate})],
    )

    async def main() -> Sequence[Event]:
        store = sqlite(":memory:")
        first = await bot.run("hi", store=store, deps=None)
        cancel.thread = first.thread
        await bot.run("echo", store=store, thread=first.thread, deps=None)
        return await logged(first.thread)

    events = asyncio.run(main())
    assert box.runs == 0
    _nothing_after_the_barrier(events, "effect_begin")


def test_a_cancel_while_a_handoff_is_prepared_starts_no_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancel = Cancel()
    ref = handoff.text_ref

    async def cancelling(rt: Runtime, text_: str) -> JsonValue:
        await cancel()
        return await ref(rt, text_)

    monkeypatch.setattr(handoff, "text_ref", cancelling)
    billing_model = scripted_model({"responses": [text("Refund issued.")]})
    billing = agent(name="billing", instructions="You handle refunds.", model=billing_model)
    front = agent(
        model=scripted_model({"responses": [text("hi"), _handoff_call()]}),
        handoffs=[billing],
    )

    async def main() -> tuple[object, Sequence[Event]]:
        store = sqlite(":memory:")
        first = await front.run("hi", store=store)
        cancel.thread = first.thread
        second = await front.run("refund", store=store, thread=first.thread)
        return second, await logged(first.thread)

    result, events = asyncio.run(main())
    assert not isinstance(result, HandedOff)
    assert not any(isinstance(e, HandoffEvent) for e in events)
    assert billing_model.sent == []
    _nothing_after_the_barrier(events, "handoff")


def test_a_cancel_while_a_handoff_is_authorized_starts_no_target() -> None:
    cancel = Cancel()

    async def gate(_call: ToolCallData, _ctx: RunContext[None]) -> ToolGate:
        await cancel()
        return {"decision": "allow"}

    billing_model = scripted_model({"responses": [text("Refund issued.")]})
    billing = agent(name="billing", instructions="You handle refunds.", model=billing_model)
    front = agent(
        model=scripted_model({"responses": [text("hi"), _handoff_call()]}),
        handoffs=[billing],
        extensions=[extension(name="ops", hooks={"before_tool": gate})],
    )

    async def main() -> tuple[object, Sequence[Event]]:
        store = sqlite(":memory:")
        first = await front.run("hi", store=store)
        cancel.thread = first.thread
        second = await front.run("refund", store=store, thread=first.thread)
        return second, await logged(first.thread)

    result, events = asyncio.run(main())
    assert not isinstance(result, HandedOff)
    assert billing_model.sent == []
    _nothing_after_the_barrier(events, "handoff")


def test_a_cancel_during_subagent_start_starts_no_child_and_keeps_the_decision() -> None:
    """The refused batch's hook decision is still recorded: the hook ran."""
    cancel = Cancel()

    async def gate(_call: ToolCallData, _ctx: RunContext[None]) -> SwitchGate:
        await cancel()
        return {"decision": "allow"}

    child_model = scripted_model({"responses": [text("child")]})
    reviewer = agent(name="reviewer", model=child_model)
    spawn: JsonValue = {
        "content": [
            {
                "type": "tool_use",
                "call_id": "c1",
                "name": "spawn_agent",
                "input": {"agent": "reviewer", "prompt": "Review."},
            }
        ],
        "stop_reason": "tool_use",
        "usage": USAGE,
    }
    lead = agent(
        model=scripted_model({"responses": [text("hi"), spawn, text("never")]}),
        subagents=[reviewer],
        extensions=[extension(name="gate", hooks={"subagent_start": gate})],
    )

    async def main() -> Sequence[Event]:
        store = sqlite(":memory:")
        first = await lead.run("hi", store=store)
        cancel.thread = first.thread
        await lead.run("review", store=store, thread=first.thread)
        return await logged(first.thread)

    events = asyncio.run(main())
    names = [e.type for e in events]
    rest = names[names.index("cancel_requested") :]
    assert "agent_spawned" not in rest
    assert "hook_decision" in rest
    assert child_model.sent == []
    ended_cancelled(events)
