"""The end-of-turn snapshot policy through agent.run: a turn that used the
sandbox ends at a fork point only when take_snapshot proved the image. The A->B / B->A schedule
(the manifest measured on the running sandbox, the image taken in between) records nothing."""

import asyncio
from collections.abc import Callable

import pytest
from pydantic import JsonValue

from threads import Completed, agent, fake_sandbox, scripted_model, sqlite
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
