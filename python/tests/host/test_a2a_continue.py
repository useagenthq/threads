"""Continuing a task: a message with a taskId answers the run's open ask_user, and only while the
task is INPUT_REQUIRED. A retried answer returns the task rather than an error, because a caller in
doubt about us must never be punished for asking again. Mirrors
typescript/packages/host/test/a2a/continue.test.ts."""

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from http import HTTPStatus

from a2a_agents import asker, talker
from a2a_kit import (
    ALICE,
    BOB,
    Agents,
    artifacts_of,
    fault_name,
    message,
    reaches,
    served,
    task,
    user_inputs,
)
from pydantic import JsonValue

ABSENT = "01a00000-0000-7000-8000-000000000000"


def run(main: Callable[[], Coroutine[object, object, None]]) -> None:
    asyncio.run(main())


def _colours() -> Agents:
    return {"support": asker("Which colour?", "painted it red", options=["red", "blue"])}


def _answer(message_id: str, text: str, found: Mapping[str, JsonValue]) -> JsonValue:
    return message(message_id, text, taskId=found["id"], contextId=found["contextId"])


def _parts(found: Mapping[str, JsonValue]) -> JsonValue:
    artifacts = artifacts_of(found)
    assert len(artifacts) == 1, artifacts
    only = artifacts[0]
    assert isinstance(only, dict)
    return only["parts"]


def test_an_answer_that_is_one_of_the_options_continues_the_run() -> None:
    async def main() -> None:
        async with served(_colours()) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "paint it"), as_=ALICE))
            open_task = await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_INPUT_REQUIRED"])
            answered = await on.rpc("SendMessage", _answer("a1", "red", open_task), as_=ALICE)
            assert answered.status_code == HTTPStatus.OK
            done = await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_COMPLETED"])
            assert _parts(done) == [{"text": "painted it red"}]

    run(main)


def test_an_answer_that_is_not_one_of_the_options_is_invalid_params_listing_them() -> None:
    async def main() -> None:
        async with served(_colours()) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "paint it"), as_=ALICE))
            open_task = await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_INPUT_REQUIRED"])
            refused = await on.rpc("SendMessage", _answer("a1", "green", open_task), as_=ALICE)
            assert fault_name(refused) == "InvalidParamsError"
            assert "red" in refused.text
            assert "blue" in refused.text
            # The question is still open, so the caller can answer it properly.
            again = await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_INPUT_REQUIRED"])
            assert again["id"] == open_task["id"]

    run(main)


def test_a_retried_continuation_returns_the_task_not_an_error() -> None:
    async def main() -> None:
        async with served(_colours()) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "paint it"), as_=ALICE))
            open_task = await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_INPUT_REQUIRED"])
            body = _answer("a1", "red", open_task)
            first = task(await on.rpc("SendMessage", body, as_=ALICE))
            again = task(await on.rpc("SendMessage", body, as_=ALICE))
            assert (again["id"], again["contextId"]) == (first["id"], first["contextId"])
            # The answer was recorded once: the run completes with the single answer it was given,
            # and the retry started no second run of its own.
            done = await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_COMPLETED"])
            assert _parts(done) == [{"text": "painted it red"}]
            assert await user_inputs(on.store) == 1

    run(main)


def test_a_continuation_reusing_a_message_id_with_another_body_is_invalid_params() -> None:
    async def main() -> None:
        async with served(_colours()) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "paint it"), as_=ALICE))
            open_task = await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_INPUT_REQUIRED"])
            task(await on.rpc("SendMessage", _answer("a1", "red", open_task), as_=ALICE))
            other = await on.rpc("SendMessage", _answer("a1", "blue", open_task), as_=ALICE)
            assert fault_name(other) == "InvalidParamsError"

    run(main)


def test_continuing_a_terminal_task_is_unsupported() -> None:
    async def main() -> None:
        async with served({"support": talker("all done")}) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "hi"), as_=ALICE))
            done = await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_COMPLETED"])
            after = await on.rpc("SendMessage", _answer("a1", "more", done), as_=ALICE)
            assert fault_name(after) == "UnsupportedOperationError"

    run(main)


def test_continuing_a_task_that_is_still_working_is_unsupported() -> None:
    async def main() -> None:
        async with served(_colours()) as on:
            # The answer to SendMessage is read back before the run can have parked, so the task is
            # not INPUT_REQUIRED yet and a continuation has nothing to answer.
            sent = task(await on.rpc("SendMessage", message("m1", "paint it"), as_=ALICE))
            early = await on.rpc("SendMessage", _answer("a1", "red", sent), as_=ALICE)
            assert fault_name(early) == "UnsupportedOperationError"
            await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_INPUT_REQUIRED"])

    run(main)


def test_continuing_another_principals_task_is_not_found() -> None:
    async def main() -> None:
        async with served(_colours()) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "paint it"), as_=ALICE))
            open_task = await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_INPUT_REQUIRED"])
            theirs = await on.rpc("SendMessage", _answer("a1", "red", open_task), as_=BOB)
            assert fault_name(theirs) == "TaskNotFoundError"

    run(main)


def test_continuing_a_task_id_that_never_existed_is_not_found() -> None:
    async def main() -> None:
        async with served(_colours()) as on:
            body = message("a1", "red", taskId=ABSENT, contextId="c")
            absent = await on.rpc("SendMessage", body, as_=ALICE)
            assert fault_name(absent) == "TaskNotFoundError"

    run(main)


def test_a_free_text_question_takes_the_messages_text_as_its_answer() -> None:
    async def main() -> None:
        async with served({"support": asker("Your order id?", "found it")}) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "where is it"), as_=ALICE))
            open_task = await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_INPUT_REQUIRED"])
            task(await on.rpc("SendMessage", _answer("a1", "order-42", open_task), as_=ALICE))
            done = await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_COMPLETED"])
            assert _parts(done) == [{"text": "found it"}]

    run(main)
