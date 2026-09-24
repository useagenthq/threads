"""The end-of-turn snapshot policy through agent.run: a turn that used the
sandbox ends at a fork point only when take_snapshot proved the image. The A->B / B->A schedule
(the manifest measured on the running sandbox, the image taken in between) records nothing."""

import asyncio
from collections.abc import Callable

import pytest
from pydantic import JsonValue

from threads import Completed, RunContext, agent, extension, fake_sandbox, scripted_model, sqlite
from threads.hooks.types import Source
from threads.log import Permissions, SnapshotData, SnapshotEvent
from threads.result import Ok
from threads.sandbox.fake import FakeSandbox
from threads.sandbox.fake_session import FakeSession
from threads.thread.snapshot import VerifiedSnapshot

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
BYPASS = Permissions(
    mode="bypass",
    allow=[],
    ask=[],
    deny=[],
    protected_paths=[],
    allow_bypass=True,
    plan_exit_mode="default",
)
FILE = "/workspace/a.txt"


def write_a() -> JsonValue:
    part: JsonValue = {
        "type": "tool_use",
        "call_id": "call_1",
        "name": "write",
        "input": {"path": "a.txt", "content": "A"},
    }
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


def done() -> JsonValue:
    content: JsonValue = [{"type": "text", "text": "Done."}]
    return {"content": content, "stop_reason": "end_turn", "usage": USAGE}


def set_to(data: bytes) -> Callable[[FakeSession], None]:
    def to(session: FakeSession) -> None:
        session.files[FILE] = data

    return to


async def run(box: FakeSandbox) -> Completed[str]:
    bot = agent(
        model=scripted_model({"responses": [write_a(), done()]}),
        sandbox=box,
        permissions=BYPASS,
    )
    result = await bot.run("go", store=sqlite(":memory:"))
    assert isinstance(result, Completed)
    return result


def test_a_turn_that_used_the_sandbox_ends_at_a_verified_fork_point() -> None:
    async def main() -> None:
        box = fake_sandbox()
        result = await run(box)
        timeline = await result.thread.timeline()
        assert isinstance(timeline, Ok)
        kinds = [e.event.type for e in timeline.value.entries]
        assert kinds[-2:] == ["turn_completed", "snapshot"]
        points = await result.thread.fork_points()
        assert isinstance(points, Ok)
        assert len(points.value) == 1
        child = await result.thread.fork(points.value[0])
        assert isinstance(child, Ok), child

    asyncio.run(main())


def test_session_start_sees_fork_on_a_forked_branchs_first_run_then_resume() -> None:
    seen: list[str] = []

    async def start(source: Source, _ctx: RunContext[None]) -> list[str]:
        seen.append(source)
        return []

    async def main() -> None:
        store = sqlite(":memory:")
        bot = agent(
            model=scripted_model({"responses": [write_a(), done(), done(), done()]}),
            sandbox=fake_sandbox(),
            permissions=BYPASS,
            extensions=[extension(name="ops", hooks={"session_start": start})],
        )
        first = await bot.run("go", store=store)
        points = await first.thread.fork_points()
        assert isinstance(points, Ok)
        child = await first.thread.fork(points.value[0])
        assert isinstance(child, Ok), child
        assert isinstance(await bot.run("more", store=store, thread=child.value), Completed)
        assert isinstance(await bot.run("again", store=store, thread=child.value), Completed)

    asyncio.run(main())
    assert seen == ["startup", "fork", "resume"]


def test_the_a_b_a_schedule_records_no_snapshot_and_no_fork_point() -> None:
    async def main() -> None:
        box = fake_sandbox()
        box.around_capture = (set_to(b"B"), set_to(b"A"))
        result = await run(box)
        timeline = await result.thread.timeline()
        assert isinstance(timeline, Ok)
        events = [e.event for e in timeline.value.entries]
        assert not any(isinstance(e, SnapshotEvent) for e in events)
        assert events[-1].type == "turn_completed"
        points = await result.thread.fork_points()
        assert points == Ok(())

    asyncio.run(main())


def test_only_verify_image_makes_a_verified_snapshot() -> None:
    raw = SnapshotData.model_validate(
        {
            "snapshot_id": "snap_1",
            "provider": "fake",
            "sandbox_id": "sbx_1",
            "capture_class": "filesystem",
            "expires_at": None,
            "manifest_hash": "0" * 64,
            "quiesced": {"frozen": [], "stopped": [], "excluded": []},
        }
    )
    with pytest.raises(TypeError):
        VerifiedSnapshot(raw, object())
