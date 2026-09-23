"""The context ladder through agent.run: L1 clearing, L2 threshold compaction with its
recorded summarizer and L3 restore, L4 preflight, before_compact, and line 0 byte-equal on every
request of the epoch (invariant 5)."""

import asyncio
from collections.abc import Sequence
from pathlib import Path

from local_sandbox import LocalSandbox
from pydantic import JsonValue

from threads import Completed, Failed, agent, scripted_model, sqlite
from threads.agents.context import RunContext
from threads.agents.results import RunResult
from threads.agents.store import open_store
from threads.hooks.extension import extension
from threads.hooks.types import CompactGate
from threads.log import (
    CompactedEvent,
    Context,
    Event,
    HookDecisionEvent,
    InjectedEvent,
    ModelRequestEvent,
    Permissions,
)
from threads.reduce.state import ReducedState
from threads.result import Ok

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
BIG = 1_000_000
BYPASS = Permissions(
    mode="bypass",
    allow=[],
    ask=[],
    deny=[],
    protected_paths=[],
    allow_bypass=True,
    plan_exit_mode="default",
)


def context(**over: JsonValue) -> Context:
    base: dict[str, JsonValue] = {
        "reserve_tokens": 0,
        "cache_ttl_ms": 300_000,
        "clear_results": {"trigger": {"tokens": BIG}, "keep_recent": 5, "exclude_tools": []},
        "spill": {
            "threshold_bytes": 32768,
            "head_bytes": 2048,
            "tail_bytes": 1024,
            "request_budget_bytes": 204800,
        },
        "compact": {"trigger": {"tokens": BIG}, "keep_tail": {"tokens": 1}, "max_failures": 3},
        "restore": {
            "max_files": 5,
            "file_tokens": 5000,
            "skill_tokens": 5000,
            "skills_total_tokens": 25000,
        },
        "max_output_continuations": 3,
        "defer_tools": "auto",
        "defer_threshold": {"permille": 100},
        "server_edits": "disabled",
    }
    return Context.model_validate({**base, **over})


def text(reply: str, input_tokens: int = 10) -> JsonValue:
    usage: JsonValue = {"input_tokens": input_tokens, "output_tokens": 2}
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": usage}


def write(path: str, body: str) -> JsonValue:
    part: JsonValue = {
        "type": "tool_use",
        "call_id": "call_1",
        "name": "write",
        "input": {"path": path, "content": body},
    }
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


async def events(result: RunResult[str]) -> list[Event]:
    timeline = await result.thread.timeline()
    assert isinstance(timeline, Ok)
    return [e.event for e in timeline.value.entries]


def kinds(events: Sequence[Event]) -> list[str]:
    return [e.type for e in events]


def test_threshold_compaction_is_recorded_restores_files_and_keeps_line_0(tmp_path: Path) -> None:
    box = LocalSandbox(tmp_path)
    script: JsonValue = {
        "responses": [
            write("notes.md", "remember the plan"),
            # The provider reports a large prompt: the next request's estimate starts here.
            text("Written.", input_tokens=1000),
            text("The user wrote notes.md."),
            text("Continuing."),
        ]
    }
    small = context(
        compact={"trigger": {"tokens": 500}, "keep_tail": {"tokens": 1}, "max_failures": 3}
    )

    async def main() -> None:
        store = sqlite(str(tmp_path / "db" / "threads.db"))
        bot = agent(model=scripted_model(script), sandbox=box, permissions=BYPASS, context=small)
        first = await bot.run("write notes", store=store)
        assert isinstance(first, Completed)
        second = await bot.run("go on", store=store, thread=first.thread)
        assert isinstance(second, Completed)
        logged = await events(second)
        compacted = next(e for e in logged if isinstance(e, CompactedEvent))
        assert compacted.data.trigger == "threshold"
        after = kinds(logged)[kinds(logged).index("compacted") :]
        assert after[:3] == ["compacted", "injected", "model_request"]
        attachment = next(
            e for e in logged if isinstance(e, InjectedEvent) and e.data.source == "attachment"
        )
        assert (attachment.data.origin.id, attachment.data.text) == (
            "notes.md",
            "remember the plan",
        )
        requests = [e for e in logged if isinstance(e, ModelRequestEvent)]
        assert [r.data.purpose for r in requests].count("compaction") == 1
        prefixes = {r.data.declared_prefix.sha256 for r in requests}
        assert len(prefixes) == 1
        sq = await open_store(store)
        last = await sq.get_artifact(requests[-1].data.request_ref.sha256)
        assert isinstance(last, Ok)
        assert b'source=\\"summary\\"' in last.value

    asyncio.run(main())


def test_the_preflight_block_makes_no_request_when_nothing_fits() -> None:
    tiny = context(reserve_tokens=200_000 - 5)
    bot = agent(model=scripted_model({"responses": [text("never")]}), context=tiny)

    async def main() -> None:
        result = await bot.run("a request that can't fit in five tokens", store=sqlite(":memory:"))
        assert isinstance(result, Failed)
        assert result.error.code == "context_exhausted"
        logged = kinds(await events(result))
        assert "context_preflight_blocked" in logged
        assert "model_request" not in logged

    asyncio.run(main())


def test_a_before_compact_deny_records_the_failure_and_the_turn_goes_on() -> None:
    async def deny(_state: ReducedState, _ctx: RunContext[None]) -> CompactGate:
        return {"decision": "deny", "reason": "not now"}

    script: JsonValue = {"responses": [text("one"), text("two")]}
    small = context(
        compact={"trigger": {"tokens": 20}, "keep_tail": {"tokens": 1}, "max_failures": 3}
    )
    ext = extension(name="ops", hooks={"before_compact": deny})

    async def main() -> None:
        store = sqlite(":memory:")
        bot = agent(model=scripted_model(script), context=small, extensions=[ext])
        first = await bot.run("first", store=store, deps=None)
        assert isinstance(first, Completed)
        second = await bot.run("second", store=store, thread=first.thread, deps=None)
        assert isinstance(second, Completed)
        logged = await events(second)
        decided = [e for e in logged if isinstance(e, HookDecisionEvent)]
        assert [(d.data.hook, d.data.decision) for d in decided] == [("before_compact", "deny")]
        assert "compaction_failed" in kinds(logged)
        assert kinds(logged)[-1] == "turn_completed"

    asyncio.run(main())


def test_l1_clears_results_an_earlier_request_rendered_and_the_pair_stays(tmp_path: Path) -> None:
    box = LocalSandbox(tmp_path)
    script: JsonValue = {"responses": [write("a.txt", "x"), text("Done."), text("Again.")]}
    clearing = context(
        clear_results={"trigger": {"tokens": 1}, "keep_recent": 0, "exclude_tools": []}
    )

    async def main() -> None:
        store = sqlite(":memory:")
        bot = agent(model=scripted_model(script), sandbox=box, permissions=BYPASS, context=clearing)
        first = await bot.run("write", store=store)
        assert isinstance(first, Completed)
        second = await bot.run("again", store=store, thread=first.thread)
        assert isinstance(second, Completed)
        logged = await events(second)
        edited = [e for e in logged if e.type == "context_edited"]
        assert len(edited) == 1
        assert kinds(logged).index("context_edited") > kinds(logged).index("tool_result")

    asyncio.run(main())
