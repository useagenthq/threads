"""Shared by the team runtime tests: scripted answers and tool calls, and reads of a team's
logs."""

import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import override

from pydantic import JsonValue, TypeAdapter

from threads import Store, Thread, scripted_model
from threads.agents.store import now_ms, open_store
from threads.log import BranchId, Event, MessageReceivedEvent, ToolResultEvent
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


class Answering(ScriptedModel):
    """A scripted model whose every answer is made from its rendered request (Render v1 text),
    so a member can reply to an ask whose id only exists at run time."""

    def __init__(self, answer: Callable[[str], JsonValue]) -> None:
        super().__init__([], {})
        self._answer = answer

    @override
    async def send(self, request: ModelRequest, context: ModelContext) -> AsyncIterator[ModelChunk]:
        made = scripted_model({"responses": [self._answer(request.body.decode())]})
        self._entries = made._entries
        async for chunk in super().send(request, context):
            yield chunk


def answers(made: Sequence[Callable[[str], JsonValue]]) -> Answering:
    """A model whose nth answer (from 0) is made from its rendered request."""
    n = [0]

    def answer(request: str) -> JsonValue:
        n[0] += 1
        return made[n[0] - 1](request) if n[0] <= len(made) else say("Nothing more.")

    return Answering(answer)


_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def result_of[T](
    log: Sequence[Event], call_id: str, wire: TypeAdapter[T] | None = None
) -> dict[str, JsonValue]:
    """The value a call's one tool_result records. With `wire`, the public result type reads it
    back field for field: the model sees exactly that type."""
    found = [e for e in log if isinstance(e, ToolResultEvent) and e.data.call_id == call_id]
    assert len(found) == 1, found
    preview = found[0].data.preview
    got = _OBJECT.validate_json(preview)
    if wire is not None:
        assert wire.dump_python(wire.validate_json(preview), mode="json") == got
    return got


def ask_ids(request: str) -> list[str]:
    """The ask ids a rendered request shows, oldest first."""
    return re.findall(r'ask_id=\\"([^\\"]+)\\"', request)


def reply_to(call_id: str, request: str, text: str) -> JsonValue:
    """The reply to the newest ask a request shows."""
    found = ask_ids(request)
    return call(call_id, "reply", {"ask_id": found[-1] if found else "", "text": text})


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
