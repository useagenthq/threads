"""Shared pieces of the Thread.compact tests: scripted replies, reading the log back and the
bytes a request sent."""

from collections.abc import Sequence

from pydantic import BaseModel, JsonValue

from threads import Parked, RunContext, agent, scripted_model, sqlite, tool
from threads.agents.store import now_ms, open_store
from threads.log import CompactionRequestedEvent, Event, ModelRequestEvent
from threads.result import Ok
from threads.thread.handle import Thread

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
SUMMARY = "The user said hi."


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def rejected(reason: str) -> JsonValue:
    return {"error": {"reason": reason, "http_status": 400}}


async def logged(thread: Thread) -> list[Event]:
    read = await (await open_store(thread.store)).read(thread.branch, now_ms())
    assert isinstance(read, Ok)
    return list(read.value.fold.events)


def requests(events: Sequence[Event], *, side: bool) -> list[ModelRequestEvent]:
    return [
        e
        for e in events
        if isinstance(e, ModelRequestEvent) and (e.data.purpose == "compaction") == side
    ]


def request_of(events: Sequence[Event]) -> CompactionRequestedEvent:
    return next(e for e in events if isinstance(e, CompactionRequestedEvent))


async def body(thread: Thread, request: ModelRequestEvent) -> str:
    got = await (await open_store(thread.store)).get_artifact(request.data.request_ref.sha256)
    assert isinstance(got, Ok)
    return got.value.decode()


def kinds(events: Sequence[Event]) -> list[str]:
    return [e.type for e in events]


class Note(BaseModel):
    text: str


USE_NOTE: JsonValue = {
    "content": [{"type": "tool_use", "call_id": "call_1", "name": "note", "input": {"text": "x"}}],
    "stop_reason": "tool_use",
    "usage": USAGE,
}


async def _note(_args: Note, _ctx: RunContext[None]) -> str:
    return "noted"


async def parked() -> Thread:
    """A thread whose turn is parked on an approval: open, with no lease held."""
    noting = tool(name="note", description="Note.", input=Note, runs="host", execute=_note)
    bot = agent(model=scripted_model({"responses": [USE_NOTE]}), tools=[noting])
    waiting = await bot.run("go", store=sqlite(":memory:"), deps=None)
    assert isinstance(waiting, Parked)
    return waiting.thread
