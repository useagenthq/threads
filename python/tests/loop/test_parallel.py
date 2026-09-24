"""Parallel tool calls (spec/schema/README.md) on the live loop: concurrent reads of one response
run together, any other call runs alone, results are recorded in call order, and Python's group
authorization comes before the group's first result."""

import asyncio
import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from kit import USER
from parallel_kit import DONE, Bodies, Setup, begin, calls, fresh, ok, results
from pydantic import JsonValue

from threads.agents.context import RunContext
from threads.hooks.extension import bind, extension
from threads.log import (
    ApprovalRequestedEvent,
    Event,
    HookDecisionEvent,
    InjectedEvent,
    PermissionDecisionEvent,
    Principal,
    ThreadId,
    ToolCallData,
    ToolCallEvent,
    ToolResultData,
    ToolResultEvent,
    ToolSpec,
)
from threads.loop.drafts import draft
from threads.loop.drive import drive
from threads.loop.runtime import Idle, Parked, Runtime
from threads.loop.tools import Dispatched, Invocation, Output, Reference
from threads.permissions import Decision
from threads.reduce import Fold
from threads.result import Ok
from threads.store import Draft
from threads.store.lines import uuid7

VECTOR = Path(__file__).resolve().parents[3] / "spec/conformance/vectors/tool-groups.json"
RECORDED_ORDER: JsonValue = json.loads(VECTOR.read_text(encoding="utf-8"))["recorded_order"]
"""The sequence both runtimes record for the replay scenario below."""


def traced(trace: list[str]):  # noqa: ANN201 - a body factory
    def body(name: str):  # noqa: ANN202 - a tool body
        async def run(_call: Invocation) -> Dispatched:
            trace.append(f"start {name}")
            await asyncio.sleep(0.001)
            trace.append(f"end {name}")
            return Output(name)

        return run

    return body


def test_two_writes_never_overlap_and_each_begins_before_dispatch() -> None:
    async def main() -> list[str]:
        trace: list[str] = []
        box: list[Runtime] = []

        async def send(_call: Invocation) -> Dispatched:
            # effect_begin is durable, and the last event, when the body starts.
            trace.append(f"begun {box[0].events[-1].type}")
            return await traced(trace)("send")(_call)

        tools = Bodies({"send": send})
        store = await fresh()
        rt = await begin(store, Setup((), ("send",)), [calls("send", "send"), DONE], tools)
        box.append(rt)
        assert await drive(rt) == Idle("end_turn")
        return trace

    trace = asyncio.run(main())
    begun = ["begun effect_begin", "start send", "end send"]
    assert trace == begun + begun


def test_a_write_runs_alone_between_two_concurrent_reads() -> None:
    """tool-exclusive-no-overlap (F1.2)."""

    async def main() -> tuple[list[str], list[str]]:
        trace: list[str] = []
        body = traced(trace)
        tools = Bodies({"read_a": body("read_a"), "send": body("send"), "read_b": body("read_b")})
        setup = Setup(("read_a", "read_b"), ("send",), concurrent=("read_a", "read_b"))
        store = await fresh()
        rt = await begin(store, setup, [calls("read_a", "send", "read_b"), DONE], tools)
        assert await drive(rt) == Idle("end_turn")
        types = [e.type for e in rt.events]
        assert types.index("effect_begin") < types.index("effect_commit")
        return trace, results(rt.events)

    trace, recorded = asyncio.run(main())
    assert trace == [
        "start read_a",
        "end read_a",
        "start send",
        "end send",
        "start read_b",
        "end read_b",
    ]
    assert recorded == ["call_1", "call_2", "call_3"]


async def _observe(_c: ToolCallData, _r: ToolResultData, _ctx: RunContext[None]) -> Sequence[str]:
    return ["seen"]


async def _reads(concurrent: bool) -> tuple[list[str], Sequence[Event]]:
    """Three reads that finish in reverse order; the second brings a reference."""
    finished: list[str] = []

    def after(name: str, seconds: float, out: Output):  # noqa: ANN202 - a tool body
        async def run(_call: Invocation) -> Dispatched:
            await asyncio.sleep(seconds)
            finished.append(name)
            return out

        return run

    noted = Output("b", references=(Reference("memory", "m1", "1", "b was read"),))
    tools = Bodies(
        {
            "a": after("a", 0.03, Output("a")),
            "b": after("b", 0.015, noted),
            "c": after("c", 0, Output("c")),
        }
    )
    setup = Setup(("a", "b", "c"), concurrent=("a", "b", "c") if concurrent else ())
    store = await fresh()
    rt = await begin(store, setup, [calls("a", "b", "c"), DONE], tools)
    principal = Principal(issuer="api", tenant="t", subject="u")
    ctx = RunContext(None, ThreadId("t"), rt.writer.branch_id, principal)
    rt = replace(rt, hooks=bind([extension(name="audit", hooks={"after_tool": _observe})], ctx))
    assert await drive(rt) == Idle("end_turn")
    return finished, rt.events


def _trace(events: Sequence[Event]) -> list[str]:
    out: list[str] = []
    for e in events:
        match e:
            case ToolCallEvent():
                out.append(f"call:{e.data.call_id}")
            case PermissionDecisionEvent():
                out.append(f"decision:{e.data.call_id}")
            case ToolResultEvent():
                out.append(f"result:{e.data.call_id}")
            case HookDecisionEvent():
                out.append(f"after_tool:{e.data.call_id}")
            case InjectedEvent():
                out.append("injected")
            case _:
                pass
    return out


def test_recorded_order_is_call_order_and_authorization_comes_first() -> None:
    """Replay: a group's log equals a sequential run's. Every call is recorded and authorized,
    in call order, before any runs; results and after_tool decisions follow in call order."""
    together, parallel = asyncio.run(_reads(concurrent=True))
    alone, sequential = asyncio.run(_reads(concurrent=False))
    assert together == ["c", "b", "a"]
    assert alone == ["a", "b", "c"]
    assert (
        _trace(parallel)
        == _trace(sequential)
        == [
            "call:call_1",
            "decision:call_1",
            "call:call_2",
            "decision:call_2",
            "call:call_3",
            "decision:call_3",
            "result:call_1",
            "after_tool:call_1",
            "result:call_2",
            "injected",
            "after_tool:call_2",
            "result:call_3",
            "after_tool:call_3",
        ]
    )

    # The same sequence TypeScript records (spec/conformance/vectors/tool-groups.json).
    shared = [
        t.replace("result:", "tool_result:")
        for t in _trace(parallel)
        if not t.startswith(("call:", "decision:"))
    ]
    assert shared == RECORDED_ORDER

    def recorded(events: Sequence[Event]) -> list[object]:
        return [e.data for e in events if isinstance(e, ToolResultEvent | InjectedEvent)]

    assert recorded(parallel) == recorded(sequential)


def test_an_ask_ends_the_group_and_the_next_call_never_starts() -> None:
    def ask_b(_fold: Fold, call: ToolCallData, _spec: ToolSpec) -> Decision:
        if call.call_id == "call_2":
            return Decision("ask", "policy", "ask_b")
        return Decision("allow", "policy", "allow_all")

    async def main() -> tuple[object, list[str], Bodies]:
        tools = Bodies({"a": ok, "b": ok, "c": ok})
        setup = Setup(("a", "b", "c"), concurrent=("a", "b", "c"))
        store = await fresh()
        rt = await begin(store, setup, [calls("a", "b", "c"), DONE], tools, ask_b)
        return await drive(rt), _trace(rt.events), tools

    halt, trace, tools = asyncio.run(main())
    assert isinstance(halt, Parked)
    assert trace == [
        "call:call_1",
        "decision:call_1",
        "call:call_2",
        "decision:call_2",
        "call:call_3",
        "decision:call_3",
        "result:call_1",
    ]
    assert dict(tools.runs) == {"a": 1}


def test_an_approved_ask_still_runs_alone() -> None:
    """Its recorded decision stays ask: after the approval it runs, then the rest group up."""

    def ask_b(_fold: Fold, call: ToolCallData, _spec: ToolSpec) -> Decision:
        verdict = "ask" if call.call_id == "call_2" else "allow"
        return Decision(verdict, "policy", "rule")

    async def main() -> list[str]:
        trace: list[str] = []
        body = traced(trace)
        tools = Bodies({n: body(n) for n in ("a", "b", "c", "d")})
        setup = Setup(("a", "b", "c", "d"), concurrent=("a", "b", "c", "d"))
        store = await fresh()
        script = [calls("a", "b", "c", "d"), DONE]
        rt = await begin(store, setup, script, tools, ask_b)
        assert isinstance(await drive(rt), Parked)
        asked = next(e for e in rt.events if isinstance(e, ApprovalRequestedEvent))
        by: dict[str, JsonValue] = {"kind": "approver", "principal": USER["principal"]}
        binding: dict[str, JsonValue] = {
            "challenge_id": asked.data.challenge_id,
            "call_id": asked.data.call_id,
            "args_hash": asked.data.args_hash,
        }
        granted = Draft("approval_granted", binding, by, True, uuid7(rt.clock()))
        address: dict[str, JsonValue] = {"kind": "approval", "id": asked.data.challenge_id}
        cause: dict[str, JsonValue] = {"address": address, "cause_event_id": granted.event_id}
        assert isinstance(await rt.append(granted, draft("resumed", cause)), Ok)
        assert await drive(rt) == Idle("end_turn")
        return trace

    trace = asyncio.run(main())
    assert trace[:6] == ["start a", "end a", "start b", "end b", "start c", "start d"]
