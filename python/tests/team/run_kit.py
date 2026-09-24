"""Shared by the team runtime tests: scripted answers and tool calls, and reads of a team's
logs."""

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import override

from pydantic import JsonValue

from threads import Store, Thread
from threads.agents.store import now_ms, open_store
from threads.log import BranchId, Event, MessageReceivedEvent
from threads.loop.model import ModelChunk, ModelContext, ModelRequest
from threads.loop.scripted import ScriptedModel
from threads.result import Ok
from threads.store import SqliteStore
from threads.team.rows import member_rows

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}


def say(text: str) -> JsonValue:
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "usage": USAGE}


def call(call_id: str, name: str, args: JsonValue) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


def start(call_id: str, agent: str, task: str) -> JsonValue:
    return call(call_id, "start", {"agent": agent, "task": task})


async def events(store: Store, thread: Thread) -> Sequence[Event]:
    read = await (await open_store(store)).read(thread.branch, now_ms())
    assert isinstance(read, Ok), read
    return read.value.fold.events


async def member_events(store: Store, team: str, name: str) -> Sequence[Event]:
    sq = await open_store(store)
    rows = await sq.run(lambda c: member_rows(c, team))
    row = next(r for r in rows if r.name == name)
    assert row.branch_id is not None
    read = await sq.read(BranchId(row.branch_id), now_ms())
    assert isinstance(read, Ok), read
    return read.value.fold.events


async def sq_of(store: Store) -> SqliteStore:
    return await open_store(store)


def types(log: Sequence[Event]) -> list[str]:
    return [e.type for e in log]


def receipts(log: Sequence[Event], kind: str) -> list[MessageReceivedEvent]:
    return [e for e in log if isinstance(e, MessageReceivedEvent) and e.data.envelope.kind == kind]


class Watched(ScriptedModel):
    """A scripted model that awaits `on(n)` before its nth request (from 1)."""

    def __init__(self, model: ScriptedModel, on: Callable[[int], Awaitable[None]]) -> None:
        super().__init__(model._entries, model._lookups)
        self._on = on
        self._n = 0

    @override
    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        self._n += 1
        await self._on(self._n)
        async for chunk in super().send(request, context):
            yield chunk
