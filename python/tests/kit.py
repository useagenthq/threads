"""Test kit for loop behaviour tests: a thread started on a fresh store, a runtime over it, and
tool runners that misbehave on purpose. Scripted models only (the guard is on)."""

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from corpus import Clock
from pydantic import JsonValue

from threads.log import BranchId, JsonObject, ThreadId, ToolCallData, ToolSpec
from threads.loop.drafts import draft
from threads.loop.model import LookupResult, LookupUnknown, Model
from threads.loop.runtime import Runtime, serving
from threads.loop.tools import Dispatched, Invocation, Termination, ToolRunner
from threads.permissions import Decision
from threads.reduce import Fold
from threads.reduce.handlers import to_json
from threads.result import Err, Ok
from threads.store import SqliteStore, StoredEvent, Writer
from threads.store.lines import uuid7

T0 = 1_790_000_000_000
USER: dict[str, JsonValue] = {
    "kind": "user",
    "principal": {"issuer": "api", "tenant": "t", "subject": "u"},
}


def allow_all(_fold: Fold, _call: ToolCallData, _spec: ToolSpec) -> Decision:
    return Decision("allow", "policy", "test_allow")


def spec(name: str, effect: str, window: int | None = None) -> JsonValue:
    data: dict[str, JsonValue] = {
        "name": name,
        "description": f"The {name} tool.",
        "effect_class": effect,
        "input_schema": {"type": "object"},
    }
    if window is not None:
        data["dedup_window_ms"] = window
    return data


def use(name: str, call_id: str = "call_1") -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": {}}
    usage: JsonValue = {"input_tokens": 5, "output_tokens": 1}
    return {"content": [part], "stop_reason": "tool_use", "usage": usage}


def text(reply: str) -> JsonValue:
    usage: JsonValue = {"input_tokens": 5, "output_tokens": 1}
    content: JsonValue = [{"type": "text", "text": reply}]
    return {"content": content, "stop_reason": "end_turn", "usage": usage}


@dataclass
class Tools:
    """A tool runner whose dispatches answer from a queue of scripted outcomes, per tool."""

    outcomes: dict[str, list[Dispatched]]
    clock: Clock
    dispatches: Counter[str] = field(default_factory=Counter[str])
    keys: list[str] = field(default_factory=list[str])
    on_dispatch: Callable[[Invocation], None] = lambda _call: None

    def invalid(self, spec: ToolSpec, input: JsonObject) -> str | None:
        return None

    async def dispatch(self, call: Invocation) -> Dispatched:
        self.on_dispatch(call)
        self.dispatches[call.spec.name] += 1
        self.keys.append(call.effect_key)
        return self.outcomes[call.spec.name].pop(0)

    async def lookup(self, call: Invocation) -> LookupResult[str]:
        return LookupUnknown(f"no lookup for {call.spec.name}")

    async def terminate(self, call: Invocation) -> Termination:
        return "unknown"

    def provider_now(self) -> int | None:
        return self.clock.now


async def open_store(path: str | Path = ":memory:") -> SqliteStore:
    opened = await SqliteStore.open(path)
    assert isinstance(opened, Ok)
    return opened.value


async def start(
    store: SqliteStore,
    specs: Sequence[JsonValue],
    model: Model,
    tools: ToolRunner,
    clock: Clock,
) -> Runtime:
    """A new thread with the given tools, its first input recorded, and a runtime over it."""
    thread, branch = ThreadId(uuid7(clock())), BranchId(uuid7(clock()))
    assert await store.create(thread, branch, clock()) == Ok(None)
    writer = await acquire(store, branch, "first", clock)
    rt = Runtime(store, writer, serving(model), tools, allow_all, clock, clock.wait_until)
    started: dict[str, JsonValue] = {
        "agent_name": "test",
        "config_hash": "0" * 64,
        "instructions": "Test.",
        "model": to_json(model.info.model),
        "model_params": dict(model.info.params),
        "adapter": to_json(model.info.adapter),
        "tools": list(specs),
    }
    user = replace(draft("user_input", {"source": "api", "text": "go"}), actor=USER)
    assert isinstance(await rt.append(draft("thread_started", started), user), Ok)
    return rt


async def acquire(store: SqliteStore, branch: BranchId, holder: str, clock: Clock) -> Writer:
    got = await store.acquire(branch, holder, clock)
    assert not isinstance(got, Err), got
    return got.value


def kinds(events: Sequence[StoredEvent]) -> list[str]:
    return [e.type for e in events]
