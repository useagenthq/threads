"""Shared pieces for the parallel tool call tests: tool bodies as coroutines, a response that
calls several tools, and a runtime whose concurrent tools are given."""

from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from corpus import Clock
from kit import T0, open_store, spec, start
from pydantic import JsonValue

from threads.log import Event, JsonObject, ToolResultEvent, ToolSpec
from threads.loop.model import LookupResult, LookupUnknown
from threads.loop.runtime import Authorize, Runtime
from threads.loop.scripted import scripted_model
from threads.loop.tools import Dispatched, Invocation, Output, Termination
from threads.store import SqliteStore

type Body = Callable[[Invocation], Awaitable[Dispatched]]

USAGE: JsonValue = {"input_tokens": 5, "output_tokens": 1}
DONE: JsonValue = {
    "content": [{"type": "text", "text": "Done."}],
    "stop_reason": "end_turn",
    "usage": USAGE,
}


def calls(*names: str) -> JsonValue:
    """One response calling `names` in order, as call_1, call_2, ..."""
    parts: list[JsonValue] = [
        {"type": "tool_use", "call_id": f"call_{i + 1}", "name": n, "input": {}}
        for i, n in enumerate(names)
    ]
    return {"content": parts, "stop_reason": "tool_use", "usage": USAGE}


async def ok(call: Invocation) -> Dispatched:
    return Output(call.spec.name)


@dataclass
class Bodies:
    """A tool runner over per-tool coroutines, counting runs per tool."""

    bodies: dict[str, Body]
    runs: Counter[str] = field(default_factory=Counter[str])

    def invalid(self, spec: ToolSpec, input: JsonObject) -> str | None:
        return None

    async def dispatch(self, call: Invocation) -> Dispatched:
        self.runs[call.spec.name] += 1
        return await self.bodies[call.spec.name](call)

    async def lookup(self, call: Invocation) -> LookupResult[str]:
        return LookupUnknown("no lookup")

    async def terminate(self, call: Invocation) -> Termination:
        return "unknown"

    def provider_now(self) -> int | None:
        return None


@dataclass(frozen=True, slots=True)
class Setup:
    """A thread whose tools are `reads` (read_only) and `writes` (unguarded)."""

    reads: Sequence[str]
    writes: Sequence[str] = ()
    concurrent: Iterable[str] = ()

    def specs(self) -> list[JsonValue]:
        return [spec(n, "read_only") for n in self.reads] + [
            spec(n, "unguarded") for n in self.writes
        ]


async def begin(
    store: SqliteStore,
    setup: Setup,
    script: Sequence[JsonValue],
    tools: Bodies,
    authorize: Authorize | None = None,
) -> Runtime:
    clock = Clock(T0)
    model = scripted_model({"responses": list(script)})
    rt = await start(store, setup.specs(), model, tools, clock)
    rt = replace(rt, concurrent=frozenset(setup.concurrent))
    return rt if authorize is None else replace(rt, authorize=authorize)


async def fresh(path: Path | str = ":memory:") -> SqliteStore:
    return await open_store(path)


def results(events: Sequence[Event]) -> list[str]:
    """The call ids of the recorded results, in log order."""
    return [e.data.call_id for e in events if isinstance(e, ToolResultEvent)]
