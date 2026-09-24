"""Shared pieces for the background-wake tests: scripted answers, a model gated on an event, and
reads of the lead's log."""

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence

from pydantic import JsonValue

from threads import (
    Agent,
    EventItem,
    ModelContext,
    ModelRequest,
    RunResult,
    Store,
    Thread,
    agent,
    scripted_model,
)
from threads.log import Event, Principal, ToolResultLateEvent, TurnCompletedEvent, WokenEvent
from threads.loop.model import ModelChunk
from threads.loop.scripted import ScriptedModel
from threads.result import Ok

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
ALICE = Principal(issuer="api", tenant="local", subject="alice")
BOB = Principal(issuer="api", tenant="local", subject="bob")


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def spawns(*names: str, first: int = 1) -> JsonValue:
    parts: list[JsonValue] = [
        {
            "type": "tool_use",
            "call_id": f"c{i}",
            "name": "spawn_agent",
            "input": {"agent": name, "prompt": "Scan.", "background": True},
        }
        for i, name in enumerate(names, first)
    ]
    return {"content": parts, "stop_reason": "tool_use", "usage": USAGE}


class Gated(ScriptedModel):
    """A scripted model whose every answer waits for `gate`; it counts its requests and sets
    `entered` on the first."""

    def __init__(self, script: Mapping[str, JsonValue], gate: asyncio.Event) -> None:
        super().__init__(scripted_model(script)._entries, {})
        self._gate = gate
        self.calls = 0
        self.entered = asyncio.Event()

    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        self.calls += 1
        self.entered.set()
        await self._gate.wait()
        async for chunk in super().send(request, context):
            yield chunk


def child(name: str, reply: str, gate: asyncio.Event) -> Agent[None, str]:
    return agent(name=name, model=Gated({"responses": [text(reply)]}, gate))


async def events_of(thread: Thread) -> list[Event]:
    timeline = await thread.timeline()
    assert isinstance(timeline, Ok)
    return [e.event for e in timeline.value.entries]


def lates(events: Sequence[Event]) -> list[str]:
    return [e.event_id for e in events if isinstance(e, ToolResultLateEvent)]


def wakes(events: Sequence[Event]) -> list[WokenEvent]:
    return [e for e in events if isinstance(e, WokenEvent)]


async def streamed(
    lead: Agent[None, str],
    text_in: str,
    gate: asyncio.Event,
    where: Store | Thread,
    principal: Principal = ALICE,
) -> RunResult[str]:
    """Runs `lead` on a new thread of a store, or on a thread, opening `gate` once its first
    turn completes."""
    stream = (
        lead.stream(text_in, store=where.store, principal=principal, thread=where, deps=None)
        if isinstance(where, Thread)
        else lead.stream(text_in, store=where, principal=principal, deps=None)
    )
    async for item in stream:
        if isinstance(item, EventItem) and isinstance(item.event, TurnCompletedEvent):
            gate.set()
    return await stream.result
