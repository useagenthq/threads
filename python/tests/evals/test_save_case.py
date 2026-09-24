"""save_case (spec/api.json, lane 22 A): any completed turn, the first included, with no sandbox
snapshot; files valid against case.schema.json; the runner replays it; what can't rerun offline
is still written, marked with why (the Python twin of test/thread/save-case.test.ts)."""

import asyncio
import json
from pathlib import Path

from eval_kit import ALLOW, LOOKUP, REFUND_TURN, Order, say, support, use
from schema_check import CASE_ID, valid

from threads import (
    CaseExpectation,
    Completed,
    agent,
    fake_sandbox,
    run_evals,
    scripted_model,
    sqlite,
)
from threads.log import EventId, Permissions, SnapshotEvent, TextPart, UserInputEvent
from threads.result import Err, Ok
from threads.thread.case import SavedCase
from threads.thread.handle import Thread

MUST = CaseExpectation(must=({"type": "tool_call", "data": {"name": "lookup_order"}},))
DONE = CaseExpectation(must=({"type": "turn_completed"},))


async def _thread(bot: object = None) -> Thread:
    run = await support(REFUND_TURN).run("Please refund order 42.", store=sqlite(":memory:"))
    assert isinstance(run, Completed)
    return run.thread


def _ok(got: Ok[SavedCase] | Err[object]) -> SavedCase:
    assert isinstance(got, Ok), got
    return got.value


async def _events(thread: Thread) -> list[object]:
    timeline = await thread.timeline()
    assert isinstance(timeline, Ok)
    return [e.event for e in timeline.value.entries]


def test_writes_every_file_each_valid_against_the_case_schema(tmp_path: Path) -> None:
    async def body() -> None:
        thread = await _thread()
        saved = _ok(
            await thread.save_case(
                "refund", expect=MUST, external_effects="stub", dir=str(tmp_path)
            )
        )
        folder = Path(saved.path)
        assert sorted(p.name for p in folder.iterdir()) == [
            "artifacts",
            "case.json",
            "expected.threads-py.json",
            "expected.threads-ts.json",
            "line0.json",
            "log.threads-py.jsonl",
            "log.threads-ts.jsonl",
            "model.json",
            "sandbox.json",
            "stubs.json",
        ]
        for name, schema in (
            ("case.json", "Case"),
            ("model.json", "ModelScript"),
            ("stubs.json", "StubScript"),
            ("sandbox.json", "SandboxScript"),
        ):
            assert valid(json.loads((folder / name).read_bytes()), f"{CASE_ID}#/$defs/{schema}")
        report = await run_evals(cases=str(tmp_path))
        assert [c.status for c in report.cases] == ["passed"]

    asyncio.run(body())


def test_a_sandbox_from_another_provider_saves_portable(tmp_path: Path) -> None:
    async def body() -> None:
        e2b = fake_sandbox()
        e2b.provider = "e2b"
        bot = agent(
            name="support", model=scripted_model({"responses": [say("Hello.")]}), sandbox=e2b
        )
        run = await bot.run("Hi", store=sqlite(":memory:"))
        saved = _ok(
            await run.thread.save_case(
                "remote", expect=DONE, external_effects="stub", dir=str(tmp_path)
            )
        )
        assert (saved.portable, saved.reason) == (True, None)
        report = await run_evals(cases=str(tmp_path))
        assert [c.status for c in report.cases] == ["passed"]

    asyncio.run(body())


def test_a_turn_after_a_snapshot_records_it_and_at_names_input_or_snapshot(tmp_path: Path) -> None:
    async def body() -> None:
        # A turn that used the sandbox ends at a fork point.
        allow = ALLOW.model_copy(update={"allow": ["write"]})
        write = use("write", {"path": "notes.txt", "content": "hi"}, "c1")
        replies = [write, say("Written."), say("Hello again.")]
        bot = agent(
            name="ops",
            model=scripted_model({"responses": replies}),
            permissions=allow,
            sandbox=fake_sandbox(),
        )
        store = sqlite(":memory:")
        first = await bot.run("Deploy.", store=store)
        await bot.run("Hi again.", store=store, thread=first.thread)
        events = await _events(first.thread)
        snapshot = next(e for e in events if isinstance(e, SnapshotEvent))
        inputs = [e for e in events if isinstance(e, UserInputEvent)]
        by_input = _ok(
            await first.thread.save_case(
                "second",
                expect=DONE,
                external_effects="stub",
                at=inputs[1].event_id,
                dir=str(tmp_path),
            )
        )
        meta = json.loads((Path(by_input.path) / "case.json").read_bytes())
        assert meta["snapshot"] == {"event_id": snapshot.event_id, "provider": "fake"}
        by_snapshot = await first.thread.save_case(
            "second-again",
            expect=DONE,
            external_effects="stub",
            at=snapshot.event_id,
            dir=str(tmp_path),
        )
        assert isinstance(by_snapshot, Ok)
        report = await run_evals(cases=str(tmp_path))
        assert [c.status for c in report.cases] == ["passed", "passed"]

    asyncio.run(body())


def test_an_input_with_content_parts_is_saved_but_not_runnable_offline(tmp_path: Path) -> None:
    async def body() -> None:
        bot = agent(name="support", model=scripted_model({"responses": [say("A cat.")]}))
        run = await bot.run([TextPart(type="text", text="What is this?")], store=sqlite(":memory:"))
        saved = _ok(
            await run.thread.save_case(
                "content", expect=DONE, external_effects="stub", dir=str(tmp_path)
            )
        )
        assert (saved.portable, saved.reason) == (False, "content_input")
        report = await run_evals(cases=str(tmp_path))
        case = report.cases[0]
        assert (case.status, case.reason) == ("skipped", "offline_not_runnable:content_input")

    asyncio.run(body())


def test_rubric_must_and_at_are_checked_before_anything_is_written(tmp_path: Path) -> None:
    async def body() -> None:
        thread = await _thread()

        async def code(
            rubric: tuple[str, ...] | None = None,
            expect: CaseExpectation = MUST,
            at: EventId | None = None,
        ) -> str:
            got = await thread.save_case(
                "refused",
                expect=expect,
                external_effects="stub",
                rubric=rubric,
                at=at,
                dir=str(tmp_path),
            )
            return "ok" if isinstance(got, Ok) else got.error.code

        assert await code(rubric=("",)) == "invalid_request"
        assert await code(rubric=("x" * 501,)) == "invalid_request"
        assert await code(rubric=tuple(f"c{i}" for i in range(21))) == "invalid_request"
        assert await code(expect=CaseExpectation(must=())) == "invalid_request"
        assert (
            await code(expect=CaseExpectation(must=({"type": "compacted"},))) == "invalid_request"
        )
        assert await code(at=EventId("0192e000-0000-7000-8000-00000000ffff")) == "invalid_request"
        assert list(tmp_path.iterdir()) == []

    asyncio.run(body())


def test_an_unfinished_turn_is_refused(tmp_path: Path) -> None:
    async def body() -> None:
        asks = Permissions.model_validate(
            {**ALLOW.model_dump(), "allow": [], "ask": ["lookup_order"]}
        )
        bot = agent(
            name="support",
            model=scripted_model({"responses": [use("lookup_order", {"id": "42"}, "c1")]}),
            tools=[LOOKUP],
            permissions=asks,
        )
        run = await bot.run("Look it up.", store=sqlite(":memory:"))
        assert run.status == "parked"
        events = await _events(run.thread)
        input = next(e for e in events if isinstance(e, UserInputEvent))
        got = await run.thread.save_case(
            "parked", expect=MUST, external_effects="stub", at=input.event_id, dir=str(tmp_path)
        )
        assert isinstance(got, Err)
        assert got.error.message == f"the turn at {input.event_id} is not completed"

    asyncio.run(body())


def test_a_v1_sandbox_file_is_read_when_each_name_ran_once(tmp_path: Path) -> None:
    async def body() -> None:
        thread = await _thread()
        folder = Path(
            _ok(
                await thread.save_case(
                    "v1", expect=MUST, external_effects="stub", dir=str(tmp_path)
                )
            ).path
        )
        v1 = {
            "tools": {
                "lookup_order": {"output": "order 42: shipped 12 days ago", "is_error": False}
            }
        }
        (folder / "sandbox.json").write_text(json.dumps(v1))
        report = await run_evals(cases=str(tmp_path))
        assert [c.status for c in report.cases] == ["passed"]

    asyncio.run(body())


__all__ = ["Order"]
