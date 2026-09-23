"""Thread.compact (spec/api.json): a request recorded while idle, carried out by the next run
before its first turn request, and answered by exactly one outcome that names it."""

import asyncio
from pathlib import Path

from compact_kit import SUMMARY, body, kinds, logged, rejected, request_of, requests, text
from pydantic import BaseModel, JsonValue

from threads import Agent, Completed, Parked, RunContext, agent, scripted_model, sqlite, tool
from threads._generated.host_api_v1 import SettingsChange
from threads.agents.store import now_ms, open_store
from threads.hooks.extension import extension
from threads.hooks.types import CompactGate
from threads.log import (
    BranchId,
    Budget,
    CompactedEvent,
    CompactionFailedEvent,
    CompactionRequestedEvent,
    ModelRef,
    Permissions,
    Principal,
    ThreadId,
)
from threads.loop.drafts import draft
from threads.reduce.state import ReducedState
from threads.result import Err, Ok
from threads.store.lines import uuid7
from threads.thread.control import LOCAL_OPERATOR
from threads.thread.handle import open_thread

OP = LOCAL_OPERATOR
OTHER_TENANT = Principal(issuer="api", tenant="acme", subject="operator")
NEXT = "a distinct next input"
BYPASS = Permissions(
    mode="bypass",
    allow=[],
    ask=[],
    deny=[],
    protected_paths=[],
    allow_bypass=True,
    plan_exit_mode="default",
)


def test_the_next_run_summarizes_up_to_the_request_first() -> None:
    script: JsonValue = {"responses": [text("Hi."), text(SUMMARY), text("Answer.")]}

    async def main() -> None:
        store = sqlite(":memory:")
        bot = agent(model=scripted_model(script))
        first = await bot.run("hi", store=store)
        asked = await first.thread.compact(OP, instructions="Keep the invoice numbers.")
        assert isinstance(asked, Ok)
        before = await logged(first.thread)
        request = request_of(before)
        assert (request.event_id, request.actor.kind) == (asked.value.event_id, "user")
        second = await bot.run(NEXT, store=store, thread=first.thread)
        assert isinstance(second, Completed)
        events = await logged(second.thread)
        (side,) = requests(events, side=True)
        assert side.data.cause_event_id == request.event_id
        sent = await body(second.thread, side)
        assert sent.endswith(
            'Additional instructions:\\nKeep the invoice numbers.","type":"text"}],"role":"user"}\n'
        )
        assert NEXT not in sent
        compacted = next(e for e in events if isinstance(e, CompactedEvent))
        assert (compacted.data.trigger, compacted.data.cause_event_id) == (
            "manual",
            request.event_id,
        )
        assert (compacted.data.from_seq, compacted.data.to_seq) == (2, request.seq - 1)
        turn = await body(second.thread, requests(events, side=False)[-1])
        assert turn.index(SUMMARY) < turn.index(NEXT)
        assert side.data.declared_prefix == requests(events, side=False)[-1].data.declared_prefix
        assert await second.thread.replay() == Ok(None)

    asyncio.run(main())


def test_a_model_change_after_the_request_sets_the_summary_request_epoch() -> None:
    script: JsonValue = {"responses": [text("Hi."), text(SUMMARY), text("Answer.")]}

    async def main() -> None:
        store = sqlite(":memory:")
        bot = agent(model=scripted_model(script))
        first = await bot.run("hi", store=store)
        assert isinstance(await first.thread.compact(OP), Ok)
        model = ModelRef(provider="scripted", name="scripted-1")
        change = SettingsChange(model=model, model_params={"max_tokens": 77})
        assert isinstance(await first.thread.set_model(change, OP), Ok)
        second = await bot.run(NEXT, store=store, thread=first.thread)
        events = await logged(second.thread)
        (side,) = requests(events, side=True)
        turns = requests(events, side=False)
        assert side.data.declared_prefix == turns[-1].data.declared_prefix
        assert side.data.declared_prefix != turns[0].data.declared_prefix
        assert '"max_tokens":77' in (await body(second.thread, side)).split("\n")[0]

    asyncio.run(main())


def test_a_second_request_waits_for_the_first_answer() -> None:
    script: JsonValue = {"responses": [text("Hi."), text(SUMMARY), text("Answer.")]}

    async def main() -> None:
        store = sqlite(":memory:")
        bot = agent(model=scripted_model(script))
        first = await bot.run("hi", store=store)
        assert isinstance(await first.thread.compact(OP), Ok)
        again = await first.thread.compact(OP)
        assert isinstance(again, Err)
        assert (again.error.code, again.error.message) == (
            "invalid_transition",
            "a compaction is already requested",
        )
        await bot.run(NEXT, store=store, thread=first.thread)
        assert isinstance(await first.thread.compact(OP), Ok)
        other = await first.thread.compact(OTHER_TENANT)
        assert isinstance(other, Err)
        assert other.error.code == "forbidden"

    asyncio.run(main())


def test_a_thread_with_no_input_has_nothing_to_compact() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        sq = await open_store(store)
        thread, branch = ThreadId(uuid7(now_ms())), BranchId(uuid7(now_ms()))
        assert await sq.create(thread, branch, now_ms()) == Ok(None)
        writer = await sq.acquire(branch, "test", now_ms)
        assert isinstance(writer, Ok)
        started: dict[str, JsonValue] = {
            "agent_name": "test",
            "config_hash": "0" * 64,
            "instructions": "Test.",
            "model": {"provider": "scripted", "name": "scripted-1"},
            "model_params": {},
            "adapter": {"name": "scripted", "version": "1", "settings": {}},
            "tools": [],
        }
        assert isinstance(await writer.value.append([draft("thread_started", started)]), Ok)
        await writer.value.release()
        opened = await open_thread(store, thread)
        assert isinstance(opened, Ok)
        empty = await opened.value.compact(OP)
        assert isinstance(empty, Err)
        assert (empty.error.code, empty.error.message) == (
            "invalid_transition",
            "nothing to compact yet",
        )

    asyncio.run(main())


class Note(BaseModel):
    text: str


def test_compact_is_refused_while_a_turn_is_open_or_parked() -> None:
    store = sqlite(":memory:")
    seen: list[str] = []

    async def note(_args: Note, ctx: RunContext[None]) -> str:
        opened = await open_thread(store, ctx.thread_id, branch_id=ctx.branch_id)
        assert isinstance(opened, Ok)
        busy = await opened.value.compact(OP)
        seen.append(busy.error.code if isinstance(busy, Err) else "ok")
        return "noted"

    use: JsonValue = {
        "content": [
            {"type": "tool_use", "call_id": "call_1", "name": "note", "input": {"text": "x"}}
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }
    noting = tool(name="note", description="Note.", input=Note, runs="host", execute=note)

    async def main() -> None:
        live = agent(
            model=scripted_model({"responses": [use, text("Done.")]}),
            tools=[noting],
            permissions=BYPASS,
        )
        done = await live.run("go", store=store, deps=None)
        assert isinstance(done, Completed)
        assert seen == ["branch_busy"]
        parked = agent(model=scripted_model({"responses": [use]}), tools=[noting])
        waiting = await parked.run("go", store=sqlite(":memory:"), deps=None)
        assert isinstance(waiting, Parked)
        busy = await waiting.thread.compact(OP)
        assert isinstance(busy, Err)
        assert busy.error.code == "branch_busy"
        assert not any(
            isinstance(e, CompactionRequestedEvent) for e in await logged(waiting.thread)
        )

    asyncio.run(main())


async def _outcome(bot: Agent[None, str]) -> tuple[list[str], CompactionFailedEvent]:
    """Runs hi, compact, then the next input; returns the log's kinds and the failure."""
    store = sqlite(":memory:")
    first = await bot.run("hi", store=store, deps=None)
    assert isinstance(await first.thread.compact(OP), Ok)
    request = request_of(await logged(first.thread))
    second = await bot.run(NEXT, store=store, thread=first.thread, deps=None)
    events = await logged(second.thread)
    failed = [e for e in events if isinstance(e, CompactionFailedEvent)]
    assert len(failed) == 1
    assert failed[0].data.cause_event_id == request.event_id
    return kinds(events), failed[0]


def test_every_failed_attempt_answers_the_request_once() -> None:
    async def main() -> None:
        for reply, reason in (
            ([text("")], "empty_summary"),
            ([rejected("prompt_too_long"), rejected("prompt_too_long")], "prompt_too_long"),
            ([rejected("server_error")], "model_error"),
        ):
            script: JsonValue = {"responses": [text("Hi."), *reply, text("Answer.")]}
            logged_kinds, failed = await _outcome(agent(model=scripted_model(script)))
            assert (failed.data.stage, failed.data.reason) == ("summary", reason)
            assert logged_kinds[-1] == "turn_completed"

    asyncio.run(main())


def test_a_budget_refusal_answers_the_request_before_the_turn_ends() -> None:
    async def main() -> None:
        budget = Budget.model_validate({"max_model_requests": 1})
        script: JsonValue = {"responses": [text("Hi.")]}
        bot = agent(model=scripted_model(script), budget=budget)
        logged_kinds, failed = await _outcome(bot)
        assert failed.data.reason == "model_error"
        tail = logged_kinds[-3:]
        assert tail == ["budget_exceeded", "compaction_failed", "turn_completed"]
        assert logged_kinds.count("model_request") == 1

    asyncio.run(main())


def test_a_before_compact_deny_answers_the_request() -> None:
    async def deny(_state: ReducedState, _ctx: RunContext[None]) -> CompactGate:
        return {"decision": "deny", "reason": "not now"}

    async def main() -> None:
        ext = extension(name="ops", hooks={"before_compact": deny})
        script: JsonValue = {"responses": [text("Hi."), text("Answer.")]}
        _, failed = await _outcome(agent(model=scripted_model(script), extensions=[ext]))
        assert (failed.data.stage, failed.data.reason) == ("hook", "hook_denied")

    asyncio.run(main())


def test_a_missing_artifact_in_the_history_is_an_artifact_error(tmp_path: Path) -> None:
    async def main() -> None:
        store = sqlite(str(tmp_path))
        bot = agent(model=scripted_model({"responses": [text("Hi.")]}))
        first = await bot.run("hi", store=store)
        sq = await open_store(store)
        gone = b"a note the history renders"
        sha = await sq.put_artifact(gone)
        writer = await sq.acquire(first.thread.branch, "test", now_ms)
        assert isinstance(writer, Ok)
        note: dict[str, JsonValue] = {
            "source": "hook",
            "trust": "untrusted_reference",
            "origin": {"id": "ops"},
            "ref": {"sha256": sha, "bytes": len(gone), "media_type": "text/plain"},
        }
        assert isinstance(await writer.value.append([draft("injected", note)]), Ok)
        await writer.value.release()
        (tmp_path / "artifacts" / "sha256" / sha[:2] / sha).unlink()
        assert isinstance(await first.thread.compact(OP), Ok)
        await bot.run(NEXT, store=store, thread=first.thread)
        events = await logged(first.thread)
        failed = next(e for e in events if isinstance(e, CompactionFailedEvent))
        assert failed.data.reason == "artifact_error"
        assert failed.data.cause_event_id == request_of(events).event_id

    asyncio.run(main())
