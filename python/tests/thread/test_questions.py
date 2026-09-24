"""ask_user end to end in one process (spec/schema/README.md, "Questions and remembered
rules"): a host-bound agent parks on the question, the asker answers strictly, the store keeps
the questions projection, an unanswered question expires, and agent.run never offers it."""

import asyncio
from dataclasses import replace

import pytest
from pydantic import JsonValue

from threads import Agent, Completed, Parked, Thread, agent, scripted_model
from threads.agents import run as run_module
from threads.agents.run import execute
from threads.agents.store import now_ms, open_store, sqlite
from threads.log import CallId, ModelRequestEvent, Principal, ResumedEvent, ToolResultEvent
from threads.result import Err, Ok
from threads.store import verify_export
from threads.store.deletion import delete_thread
from threads.thread.control import LOCAL_OPERATOR

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
BOB = Principal(issuer="api", tenant="local", subject="bob")
COLOR: JsonValue = {"question": "Which color?", "options": ["red", "blue"]}
DAY = 86_400_000


def _text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def _ask(args: JsonValue) -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": "call_1", "name": "ask_user", "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


def _drop(_item: object) -> None:
    pass


def _bot(args: JsonValue = COLOR) -> Agent[None, str]:
    return agent(model=scripted_model({"responses": [_ask(args), _text("Done.")]}))


async def _asked(bot: Agent[None, str]) -> Thread:
    """The run a host starts for an HTTP API call: ask_user is offered, and it parks."""
    hosted = replace(bot.definition, answerer=True)
    parked = await execute(hosted, "Deploy it.", {"store": sqlite(":memory:")}, None, _drop)
    assert isinstance(parked, Parked), parked
    assert parked.reason == "awaiting_input"
    return parked.thread


async def _rows(thread: Thread) -> list[tuple[str, str]]:
    sq = await open_store(thread.store)
    rows = await sq.run(lambda c: c.execute("SELECT call_id, state FROM questions").fetchall())
    return [(str(r[0]), str(r[1])) for r in rows]


async def _results(thread: Thread) -> list[ToolResultEvent]:
    read = await (await open_store(thread.store)).read(thread.branch, 0)
    assert isinstance(read, Ok)
    return [e for e in read.value.fold.events if isinstance(e, ToolResultEvent)]


def test_the_asker_answers_by_number_and_the_run_completes() -> None:
    async def main() -> None:
        bot = _bot()
        thread = await _asked(bot)
        assert await _rows(thread) == [("call_1", "open")]
        refused = await thread.answer(CallId("call_1"), "2", BOB)
        assert isinstance(refused, Err)
        assert refused.error.code == "forbidden"
        done = await thread.answer(CallId("call_1"), "2", LOCAL_OPERATOR)
        assert isinstance(done, Ok), done
        assert (await _results(thread))[-1].data.preview == "blue"
        assert await _rows(thread) == [("call_1", "answered")]
        hosted = replace(bot.definition, answerer=True)
        resumed = await execute(hosted, None, {"thread": thread}, None, _drop)
        assert isinstance(resumed, Completed)

    asyncio.run(main())


def test_an_answer_that_is_no_option_is_invalid_and_the_question_stays_open() -> None:
    async def main() -> None:
        thread = await _asked(_bot())
        refused = await thread.answer(CallId("call_1"), "green", LOCAL_OPERATOR)
        assert isinstance(refused, Err)
        assert refused.error.code == "invalid_answer"
        assert refused.error.message == "answer one of: red, blue"
        assert await _results(thread) == []
        assert await _rows(thread) == [("call_1", "open")]

    asyncio.run(main())


def test_a_missing_row_never_blocks_an_answer() -> None:
    async def main() -> None:
        thread = await _asked(_bot())
        sq = await open_store(thread.store)
        await sq.run(lambda c: c.execute("DELETE FROM questions"))
        done = await thread.answer(CallId("call_1"), ["RED"], LOCAL_OPERATOR)
        assert isinstance(done, Ok)
        assert (await _results(thread))[-1].data.preview == "red"

    asyncio.run(main())


def test_an_unanswered_question_expires_and_the_turn_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def main() -> None:
        bot = _bot()
        thread = await _asked(bot)
        late = now_ms() + DAY + 1
        monkeypatch.setattr(run_module, "now_ms", lambda: late)
        hosted = replace(bot.definition, answerer=True)
        resumed = await execute(hosted, None, {"thread": thread}, None, _drop)
        assert isinstance(resumed, Completed)
        (expired,) = await _results(thread)
        assert (expired.data.origin, expired.data.preview) == ("not_executed", "no answer")
        assert await _rows(thread) == [("call_1", "expired")]

    asyncio.run(main())


def test_invalid_options_close_the_call_without_parking() -> None:
    async def main() -> None:
        hosted = replace(_bot({"question": "Ok?", "options": ["Yes", " yes "]}).definition)
        hosted = replace(hosted, answerer=True)
        done = await execute(hosted, "Go.", {"store": sqlite(":memory:")}, None, _drop)
        assert isinstance(done, Completed)
        (closed,) = await _results(done.thread)
        assert closed.data.is_error
        assert closed.data.preview.startswith("invalid input: ")

    asyncio.run(main())


def test_agent_run_never_offers_ask_user() -> None:
    names = {s.name for s in _bot().definition.specs()}
    assert "ask_user" not in names
    assert "ask_user" in {s.name for s in replace(_bot().definition, answerer=True).specs()}


def test_deleting_the_thread_deletes_its_question_rows() -> None:
    async def main() -> None:
        thread = await _asked(_bot())
        sq = await open_store(thread.store)
        tenant, now = thread.store.tenant, now_ms()
        assert await sq.run(lambda c: delete_thread(c, tenant, thread.id, now)) == Ok(1)
        assert await _rows(thread) == []

    asyncio.run(main())


def test_an_imported_open_question_gets_its_row_and_can_be_answered() -> None:
    async def main() -> None:
        thread = await _asked(_bot())
        exported = await (await open_store(thread.store)).export(thread.branch)
        assert isinstance(exported, Ok)
        verified = verify_export(exported.value, now_ms())
        assert isinstance(verified, Ok)
        other = replace(thread, store=sqlite(":memory:"))
        source, dest = await open_store(thread.store), await open_store(other.store)
        for event in verified.value.fold.events:
            if isinstance(event, ModelRequestEvent):
                ref = await source.get_artifact(event.data.request_ref.sha256)
                assert isinstance(ref, Ok)
                await dest.put_artifact(ref.value)
        imported = await dest.import_log(verified.value)
        assert imported == Ok(None)
        assert await _rows(other) == [("call_1", "open")]
        done = await other.answer(CallId("call_1"), "red", LOCAL_OPERATOR)
        assert isinstance(done, Ok)
        assert await _rows(other) == [("call_1", "answered")]

    asyncio.run(main())


def test_an_answer_racing_the_expiry_settles_the_question_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def main() -> None:
        bot = _bot()
        thread = await _asked(bot)
        late = now_ms() + DAY + 1
        monkeypatch.setattr(run_module, "now_ms", lambda: late)
        hosted = replace(bot.definition, answerer=True)
        await asyncio.gather(
            thread.answer(CallId("call_1"), "red", LOCAL_OPERATOR),
            execute(hosted, None, {"thread": thread}, None, _drop),
            return_exceptions=True,
        )
        read = await (await open_store(thread.store)).read(thread.branch, 0)
        assert isinstance(read, Ok)
        settled = [r for r in await _results(thread) if r.data.call_id == "call_1"]
        resumes = [e for e in read.value.fold.events if isinstance(e, ResumedEvent)]
        assert (len(settled), len(resumes)) == (1, 1)

    asyncio.run(main())
