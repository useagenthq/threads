"""Stopping inside a group (spec/schema/README.md, Parallel tool calls): a cancel is a barrier after
the started reads, a lost lease records nothing more and cancels the started bodies, a crash
re-runs only the unrecorded reads, and at most 8 calls are started and unrecorded at once."""

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from corpus import Clock
from kit import USER, acquire
from parallel_kit import DONE, Bodies, Setup, begin, calls, fresh, ok, results
from test_invariants import take_over

from threads.log import Event, ToolResultEvent, TurnCompletedEvent
from threads.loop.drafts import draft
from threads.loop.drive import drive
from threads.loop.parallel import WINDOW
from threads.loop.runtime import Failed, Idle, Runtime
from threads.loop.tools import Dispatched, Invocation, Output
from threads.result import Ok

NAMES = tuple(f"r{i}" for i in range(1, 21))


def _closed(events: Sequence[Event]) -> list[tuple[str, str]]:
    return [(e.data.origin, e.data.preview) for e in events if isinstance(e, ToolResultEvent)]


async def _resume(rt: Runtime, tools: Bodies) -> Runtime:
    """The next owner, once the stale lease expired: same log, same tools."""
    clock = Clock(rt.clock() + 60_000)
    writer = await acquire(rt.store, rt.writer.branch_id, "next", clock)
    return replace(rt, writer=writer, clock=clock, tools=tools)


def test_a_cancel_lets_started_reads_finish_and_queued_ones_never_start() -> None:
    names = NAMES[:10]

    async def main() -> tuple[object, Sequence[Event], Bodies]:
        gate = asyncio.Event()
        box: list[Runtime] = []

        async def body(_call: Invocation) -> Dispatched:
            if sum(tools.runs.values()) == WINDOW:
                # The window is full: cancel while all 8 wait on the gate.
                cancel = replace(draft("cancel_requested", {"scope": "turn"}), actor=USER)
                assert isinstance(await box[0].append(cancel), Ok)
                gate.set()
            await gate.wait()
            return Output("read")

        tools = Bodies(dict.fromkeys(names, body))
        store = await fresh()
        rt = await begin(store, Setup(names, concurrent=names), [calls(*names), DONE], tools)
        box.append(rt)
        return await drive(rt), rt.events, tools

    halt, events, tools = asyncio.run(main())
    assert halt == Idle("cancelled")
    assert sorted(tools.runs) == sorted(names[:8])
    assert results(events) == [f"call_{i}" for i in range(1, 11)]
    closed = _closed(events)
    assert closed[:8] == [("executed", "read")] * 8
    assert closed[8:] == [("not_executed", "not executed: cancelled")] * 2
    assert isinstance(events[-1], TurnCompletedEvent)


def test_a_lost_lease_never_invokes_the_next_body_and_cancels_the_started_one(
    tmp_path: Path,
) -> None:
    path = tmp_path / "threads.db"
    names = NAMES[:3]

    async def main() -> tuple[object, Sequence[Event], list[str], Bodies]:
        seen: list[str] = []

        async def stuck(_call: Invocation) -> Dispatched:
            take_over(path)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                seen.append("cancelled")
                raise
            return Output("never")

        tools = Bodies({"r1": stuck, "r2": ok, "r3": ok})
        store = await fresh(path)
        try:
            rt = await begin(store, Setup(names, concurrent=names), [calls(*names), DONE], tools)
            halt = await drive(rt)
            events = rt.events
            # The next owner runs the unrecorded reads in call order, once each.
            recovered = Bodies(dict.fromkeys(names, ok))
            again = await _resume(rt, recovered)
            assert await drive(again) == Idle("end_turn")
            assert results(again.events) == ["call_1", "call_2", "call_3"]
            assert dict(recovered.runs) == dict.fromkeys(names, 1)
            return halt, events, seen, tools
        finally:
            await store.close()

    halt, events, seen, tools = asyncio.run(main())
    assert isinstance(halt, Failed)
    assert halt.code == "branch_busy"
    assert dict(tools.runs) == {"r1": 1}
    assert seen == ["cancelled"]
    assert results(events) == []


def test_a_crash_after_result_1_re_runs_calls_2_and_3_once(tmp_path: Path) -> None:
    path = tmp_path / "threads.db"
    names = NAMES[:3]

    async def main() -> tuple[object, list[str], Bodies]:
        tools = Bodies(dict.fromkeys(names, ok))
        store = await fresh(path)
        try:
            rt = await begin(store, Setup(names, concurrent=names), [calls(*names), DONE], tools)

            def crash(batch: Sequence[Event]) -> None:
                if any(isinstance(e, ToolResultEvent) for e in batch):
                    take_over(path)

            halt = await drive(replace(rt, observe=crash))
            again = await _resume(rt, tools)
            assert await drive(again) == Idle("end_turn")
            return halt, results(again.events), tools
        finally:
            await store.close()

    halt, recorded, tools = asyncio.run(main())
    assert isinstance(halt, Failed)
    assert recorded == ["call_1", "call_2", "call_3"]
    assert dict(tools.runs) == {"r1": 1, "r2": 2, "r3": 2}


def test_at_most_8_calls_are_started_and_unrecorded() -> None:
    async def main() -> tuple[int, int, list[str]]:
        state = {"outstanding": 0, "most": 0, "started_at_first": 0}

        async def body(call: Invocation) -> Dispatched:
            state["outstanding"] += 1
            state["most"] = max(state["most"], state["outstanding"])
            await asyncio.sleep(0.03 if call.call_id == "call_1" else 0.001)
            return Output("ok")

        def recorded(batch: Sequence[Event]) -> None:
            for e in batch:
                if isinstance(e, ToolResultEvent):
                    state["outstanding"] -= 1
                    if e.data.call_id == "call_1":
                        state["started_at_first"] = sum(tools.runs.values())

        tools = Bodies(dict.fromkeys(NAMES, body))
        store = await fresh()
        rt = await begin(store, Setup(NAMES, concurrent=NAMES), [calls(*NAMES), DONE], tools)
        assert await drive(replace(rt, observe=recorded)) == Idle("end_turn")
        return state["most"], state["started_at_first"], results(rt.events)

    most, started_at_first, recorded = asyncio.run(main())
    assert most == WINDOW
    assert started_at_first == WINDOW
    assert recorded == [f"call_{i}" for i in range(1, 21)]
