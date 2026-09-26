"""What every per-hook case shares: the scripted responses, the one echo tool, the one "ops"
extension, and the normalization the cross-language vector compares
(spec/conformance/vectors/hook-decisions.json)."""

import json
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import replace

from pydantic import BaseModel, JsonValue

from threads import Model, RunContext, scripted_model, tool
from threads.agents.store import now_ms, open_store
from threads.agents.tool import Tool
from threads.hooks.extension import Extension, extension
from threads.hooks.types import Hooks
from threads.log import Event, HookDecisionEvent, ModelRef, Retry
from threads.loop.scripted import SCRIPTED_INFO, ScriptedModel
from threads.result import Ok
from threads.thread.handle import Thread

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
OVERLOADED: JsonValue = {"error": {"reason": "overloaded", "http_status": 529}}
EXTENSION = "ops"
"""Every case names its extension "ops", as the vector records."""

FAILURE = "<failure>"
"""A failure's reason is the host's own error text, which each language words its own way."""


def say(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def part(call_id: str, name: str = "echo", args: JsonValue | None = None) -> JsonValue:
    return {
        "type": "tool_use",
        "call_id": call_id,
        "name": name,
        "input": {"text": "hi"} if args is None else args,
    }


def uses(*parts: JsonValue) -> JsonValue:
    return {"content": list(parts), "stop_reason": "tool_use", "usage": USAGE}


def use(call_id: str = "call_1") -> JsonValue:
    return uses(part(call_id))


class Echo(BaseModel):
    text: str


class Box:
    """The echo tool, counting how often its body ran."""

    def __init__(self) -> None:
        self.runs = 0

    async def _run(self, args: Echo, _ctx: RunContext[None]) -> str:
        self.runs += 1
        return f"echo {args.text} SECRET=hunter2"

    @property
    def tool(self) -> Tool[Echo, str, None]:
        return tool(
            name="echo",
            description="Echo.",
            input=Echo,
            runs="host",
            effect="read_only",
            execute=self._run,
        )


def ops(hooks: Hooks[None]) -> Extension[None]:
    return extension(name=EXTENSION, hooks=hooks)


class Renamed(ScriptedModel):
    """A scripted model under another name, so policy.models lists two of them and a fallback
    is a new settings epoch (the shape tests/evals/test_hook_kinds.py already uses)."""

    def __init__(self, responses: Sequence[JsonValue]) -> None:
        inner = scripted_model({"responses": list(responses)})
        super().__init__([], {})
        self._entries = inner._entries
        ref = ModelRef(provider="scripted", name="scripted-small")
        limits = SCRIPTED_INFO.limits.model_copy(update={"name": "scripted-small"})
        self._info = replace(SCRIPTED_INFO, model=ref, limits=limits)


def small(responses: Sequence[JsonValue]) -> Model:
    """The fallback model: the scripted one, renamed (TypeScript's `scriptedSmall`)."""
    return Renamed(responses)


async def logged(thread: Thread) -> list[Event]:
    read = await (await open_store(thread.store)).read(thread.branch, now_ms())
    assert isinstance(read, Ok)
    return list(read.value.fold.events)


def kinds(events: Sequence[Event]) -> list[str]:
    return [e.type for e in events]


def last_at(order: Sequence[str], kind: str) -> int:
    """The index of the last `kind` in `order`."""
    return len(order) - 1 - order[::-1].index(kind)


type Decision = Mapping[str, str | int]
"""One `hook_decision` as the shared vector holds it."""


def quick(*, max_retries: int = 8, fallback_after: int = 3) -> Retry:
    """The retry policy the cases run under: the waits are 1 ms, so a test never sleeps."""
    return Retry(
        max_retries=max_retries,
        base_delay_ms=1,
        max_delay_ms=1,
        max_retry_after_ms=60_000,
        max_total_wait_ms=600_000,
        crash_resends=2,
        fallback_after=fallback_after,
        fallback_scope="turn",
        heartbeat_ms=15_000,
    )


type HookCase = Callable[[], Coroutine[None, None, Sequence[Decision]]]
"""A case: it drives one public run, asserts what the hook did, and returns its decisions."""

_IDS = ("call_id", "request_event_id", "input_event_id")


def _at(known: Mapping[str, int], value: str, field: str) -> int:
    if value not in known:
        raise AssertionError(f"{field} {value} names no event in the branch")
    return known[value]


def normalize(events: Sequence[Event]) -> list[Decision]:
    """Every `hook_decision` of a branch, in log order: the hook's wire name, its decision, the
    extension and reason, with every id field replaced by the 0-based position, in the branch's
    log, of the event it names."""
    by_event: dict[str, int] = {str(e.event_id): i for i, e in enumerate(events)}
    by_call: dict[str, int] = {
        str(e.data.call_id): i for i, e in enumerate(events) if e.type == "tool_call"
    }
    rows: list[Decision] = []
    for event in events:
        if not isinstance(event, HookDecisionEvent):
            continue
        data: dict[str, str | int] = json.loads(event.data.model_dump_json(exclude_none=True))
        if data["decision"] == "failed" and "reason" in data:
            data["reason"] = FAILURE
        for field in _IDS:
            if field in data:
                table = by_call if field == "call_id" else by_event
                data[field] = _at(table, str(data[field]), field)
        rows.append(data)
    return rows


def only_decisions_differ(observed: Sequence[Event], plain: Sequence[Event]) -> None:
    """An observer changed nothing else: the run's events, minus the decisions its own failure
    added, are the events of the same run without the extension."""
    assert [k for k in kinds(observed) if k != "hook_decision"] == kinds(plain)
