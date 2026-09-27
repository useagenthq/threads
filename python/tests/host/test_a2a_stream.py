"""The streams. Every frame is a function of the committed run slice, so a resume at any frame
boundary yields exactly the frames after it: the assertion below replays from every boundary and
demands the remainder, with no frame delivered twice and none skipped. Mirrors
typescript/packages/host/test/a2a/stream.test.ts."""

import asyncio
import json
from collections.abc import Callable, Coroutine, Sequence
from typing import Final

from a2a_agents import asker, talker
from a2a_kit import (
    ALICE,
    Agents,
    fault_name,
    frames,
    item_of,
    message,
    reaches,
    served,
    shape,
    task,
)
from pydantic import JsonValue

CURSOR_FIELDS: Final = 2
"""A frame id is `<seq>:<k>`."""
MANY_FRAMES: Final = 2
"""Below this the resume assertion would not exercise a middle boundary at all."""


def run(main: Callable[[], Coroutine[object, object, None]]) -> None:
    asyncio.run(main())


def _parked() -> Agents:
    """A task parked on a question: interrupted, not terminal, so it can still be subscribed to."""
    return {"support": asker("Which colour?", "done", options=["red", "blue"])}


def test_a_streaming_sends_frames_are_a_snapshot_then_status_changes_then_the_artifact() -> None:
    async def main() -> None:
        async with served({"support": talker("the answer")}) as on:
            answered = await on.rpc("SendStreamingMessage", message("m1", "hi"), as_=ALICE)
            assert answered.headers["content-type"].startswith("text/event-stream")
            read = frames(answered)
            assert shape(read) == [
                "task",
                "status:TASK_STATE_WORKING",
                "status:TASK_STATE_COMPLETED",
                "artifact",
            ]
            # Every frame carries a resumable position, and they only ever move forwards.
            ids = [f[0] or "" for f in read]
            assert all(len(at.split(":")) == CURSOR_FIELDS for at in ids)
            assert len(set(ids)) == len(ids)

    run(main)


def test_a_resume_at_every_frame_boundary_yields_exactly_the_remaining_frames() -> None:
    async def main() -> None:
        async with served(_parked()) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "hi"), as_=ALICE))
            task_id = str(sent["id"])
            await reaches(on, ALICE, task_id, ["TASK_STATE_INPUT_REQUIRED"])

            async def whole(start: str = "0:0") -> list[tuple[str | None, JsonValue]]:
                return frames(
                    await on.http(
                        "GET",
                        f"/tasks/{task_id}:subscribe",
                        as_=ALICE,
                        headers={"last-event-id": start},
                    )
                )

            everything = await whole()
            assert len(everything) > MANY_FRAMES
            for at, frame in enumerate(everything):
                rest = await whole(frame[0] or "")
                # Exactly what follows this frame: nothing repeated, nothing lost.
                assert [f[0] for f in rest] == [f[0] for f in everything[at + 1 :]]
                assert [json.dumps(f[1]) for f in rest] == [
                    json.dumps(f[1]) for f in everything[at + 1 :]
                ]
            # And a resume past every frame yields nothing and closes.
            assert await whole(everything[-1][0] or "") == []

    run(main)


def test_the_same_committed_run_streams_identical_frames_every_time() -> None:
    async def main() -> None:
        async with served(_parked()) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "hi"), as_=ALICE))
            task_id = str(sent["id"])
            await reaches(on, ALICE, task_id, ["TASK_STATE_INPUT_REQUIRED"])
            reads: list[str] = []
            for _ in range(2):
                answered = await on.http(
                    "GET",
                    f"/tasks/{task_id}:subscribe",
                    as_=ALICE,
                    headers={"last-event-id": "0:0"},
                )
                reads.append(json.dumps(frames(answered)))
            assert reads[0] == reads[1]

    run(main)


def test_a_malformed_last_event_id_is_invalid_params() -> None:
    async def main() -> None:
        async with served({"support": asker("Which?", "done")}) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "hi"), as_=ALICE))
            answered = await on.http(
                "GET",
                f"/tasks/{sent['id']}:subscribe",
                as_=ALICE,
                headers={"last-event-id": "not-a-cursor"},
            )
            assert fault_name(answered) == "InvalidParamsError"

    run(main)


def test_subscribe_on_a_terminal_task_is_unsupported_and_get_task_then_has_the_result() -> None:
    async def main() -> None:
        async with served({"support": talker("the answer")}) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "hi"), as_=ALICE))
            await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_COMPLETED"])
            late = await on.rpc("SubscribeToTask", {"id": sent["id"]}, as_=ALICE)
            assert fault_name(late) == "UnsupportedOperationError"
            # The required fallback: a reconnecting follower reads the finished task instead.
            got = task(await on.rpc("GetTask", {"id": sent["id"]}, as_=ALICE))
            artifacts = got["artifacts"]
            assert isinstance(artifacts, list)
            only = artifacts[0]
            assert isinstance(only, dict)
            assert only["parts"] == [{"text": "the answer"}]

    run(main)


def test_the_subscribe_path_accepts_both_get_and_post() -> None:
    async def main() -> None:
        async with served({"support": asker("Which?", "done")}) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "hi"), as_=ALICE))
            await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_INPUT_REQUIRED"])
            for verb in ("GET", "POST"):
                answered = await on.http(verb, f"/tasks/{sent['id']}:subscribe", as_=ALICE)
                assert answered.headers["content-type"].startswith("text/event-stream"), verb

    run(main)


def _wrapped(read: Sequence[tuple[str | None, JsonValue]], rpc_id: str) -> bool:
    return all(
        isinstance(data, dict) and data.get("jsonrpc") == "2.0" and data.get("id") == rpc_id
        for _, data in read
    )


def test_json_rpc_wraps_each_stream_item_in_its_envelope_and_http_json_does_not() -> None:
    async def main() -> None:
        async with served({"support": talker("the answer", "the answer")}) as on:
            rpc = frames(
                await on.rpc("SendStreamingMessage", message("m1", "hi"), as_=ALICE, rpc_id="s-1")
            )
            assert _wrapped(rpc, "s-1")
            http = frames(
                await on.http("POST", "/message:stream", as_=ALICE, body=message("m2", "hi"))
            )
            # Bare StreamResponse items: no envelope, no id, no jsonrpc.
            for _, data in http:
                assert isinstance(data, dict)
                assert "jsonrpc" not in data
                item_of(data)
            assert shape(http) == shape(rpc)

    run(main)


def test_a_stream_closes_at_an_interrupted_state_so_a_question_does_not_hold_it_open() -> None:
    async def main() -> None:
        async with served(_parked()) as on:
            read = frames(await on.rpc("SendStreamingMessage", message("m1", "hi"), as_=ALICE))
            assert shape(read)[-1] == "status:TASK_STATE_INPUT_REQUIRED"
            # The question and its options travelled in the frame, so a client can answer from it.
            last = item_of(read[-1][1])
            assert "Which colour?" in json.dumps(last["statusUpdate"])

    run(main)


def test_a_streaming_send_that_is_refused_answers_the_refusal_not_a_stream() -> None:
    async def main() -> None:
        async with served({"support": talker("hi")}) as on:
            body: JsonValue = {
                "message": {"messageId": "f1", "role": "ROLE_USER", "parts": [{"raw": "AAA"}]}
            }
            answered = await on.rpc("SendStreamingMessage", body, as_=ALICE)
            assert fault_name(answered) == "ContentTypeNotSupportedError"

    run(main)


def test_a_rejected_streaming_send_is_one_frame_and_closes() -> None:
    async def main() -> None:
        async with served({"support": talker("hi")}) as on:
            from threadsai.a2a.protocol import PROVENANCE  # noqa: PLC0415 - one test needs the uri

            body = message("m1", "hi", metadata={PROVENANCE: {"hops": 9}})
            read = frames(await on.rpc("SendStreamingMessage", body, as_=ALICE))
            # A task refused before it existed has no log to follow: its one frame is the stream.
            assert shape(read) == ["task"]
            assert read[0][0] is None

    run(main)
