"""Shared by the Thread usage and cost tests: priced scripted runs and ways to break their logs."""

import asyncio
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import replace

from pydantic import JsonValue, TypeAdapter

from threads import Agent, Completed, Store, agent, sqlite
from threads.agents.store import open_store
from threads.log import Cost, OutputPart, ThreadId, Usage
from threads.log import Model as ModelLimits
from threads.loop.model import Model, ModelInfo, ModelResponse
from threads.loop.scripted import ScriptedModel
from threads.result import Ok
from threads.store.conn import Conn
from threads.thread.handle import Thread

PRICE: JsonValue = {"input": 3000, "output": 15_000}
ONE = 10 * 3000 + 2 * 15_000
"""One scripted response of 10 input and 2 output tokens at PRICE."""
USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
_PARTS: TypeAdapter[list[OutputPart]] = TypeAdapter(list[OutputPart])


class Priced(ScriptedModel):
    """A scripted model that declares a price; still scripted, so the request guard allows it."""

    price: JsonValue = PRICE

    @property
    def info(self) -> ModelInfo:
        base = super().info
        limits = {**base.limits.model_dump(mode="json"), "price": self.price}
        return replace(base, limits=ModelLimits.model_validate(limits))


def say(text: str, usage: JsonValue = USAGE) -> ModelResponse:
    parts = _PARTS.validate_python([{"type": "text", "text": text}])
    return ModelResponse(parts, "end_turn", Usage.model_validate(usage), None)


def spawn(name: str) -> ModelResponse:
    use = {"type": "tool_use", "call_id": f"s-{name}", "name": "spawn_agent"}
    parts = _PARTS.validate_python([{**use, "input": {"agent": name, "prompt": "Do it."}}])
    return ModelResponse(parts, "tool_use", Usage.model_validate(USAGE), None)


def usd(known: int, *, exact: bool) -> Cost:
    """A USD total whose upper bound is its known cost (every attempt here settles)."""
    return Cost(
        currency="USD",
        known_nanos=known,
        upper_bound_nanos=known,
        complete=exact,
        bounded=exact,
    )


def priced(*replies: ModelResponse, price: JsonValue = PRICE) -> Priced:
    model = Priced(list(replies), {})
    model.price = price
    return model


def unpriced(*replies: ModelResponse) -> ScriptedModel:
    return ScriptedModel(list(replies), {})


async def run(store: Store, model: Model, subagents: Sequence[Agent[None, str]] = ()) -> Thread:
    result = await agent(name="lead", model=model, subagents=list(subagents)).run(
        "Go.", store=store
    )
    assert isinstance(result, Completed)
    return result.thread


async def corrupt(store: Store, thread: ThreadId, prompt: str) -> None:
    """Rewrites one stored line of `thread`'s main branch so its chain no longer verifies."""
    sq = await open_store(store)
    root = await sq.root(thread)
    assert isinstance(root, Ok)
    sql = (
        "UPDATE events SET line = CAST(replace(CAST(line AS TEXT), ?, 'Edited.') AS BLOB)"
        " WHERE branch_id = ? AND seq = 2"
    )

    def edit(c: Conn) -> None:
        c.execute(sql, (prompt, root.value))

    await sq.run(edit)


async def child_ids(thread: Thread) -> tuple[ThreadId, ...]:
    """The thread's children, in spawn order."""
    children = await thread.children()
    assert isinstance(children, Ok)
    return tuple(c.child_thread_id for c in children.value)


def check(body: Callable[[Store], Coroutine[None, None, None]]) -> None:
    asyncio.run(body(sqlite(":memory:")))


async def unpriced_middle(store: Store) -> Thread:
    """A priced lead whose child "mid" has no price and spawns a priced "leaf"."""
    leaf = agent(name="leaf", model=priced(say("Leaf.")))
    mid = agent(name="mid", model=unpriced(spawn("leaf"), say("Mid.")), subagents=[leaf])
    return await run(store, priced(spawn("mid"), say("Done.")), [mid])
