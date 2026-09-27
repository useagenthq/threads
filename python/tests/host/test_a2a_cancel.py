"""CancelTask is the durable barrier on the task's thread. It answers the task in its current
state, which becomes CANCELED once the barrier applies, and it never claims to undo what already
happened. A task that has already ended cannot be cancelled at all. Mirrors
typescript/packages/host/test/a2a/cancel.test.ts."""

import asyncio
from collections.abc import Callable, Coroutine
from http import HTTPStatus

from a2a_agents import asker, talker
from a2a_kit import ALICE, BOB, Agents, fault_name, message, reaches, served, state_of, task


def run(main: Callable[[], Coroutine[object, object, None]]) -> None:
    asyncio.run(main())


def _waiting() -> Agents:
    return {"support": asker("Which colour?", "done")}


def test_cancel_on_a_running_task_answers_the_task_and_it_ends_canceled() -> None:
    async def main() -> None:
        async with served(_waiting()) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "hi"), as_=ALICE))
            await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_INPUT_REQUIRED"])
            answered = await on.rpc("CancelTask", {"id": sent["id"]}, as_=ALICE)
            assert answered.status_code == HTTPStatus.OK
            # The answer is the task, not a bare acknowledgement.
            assert task(answered)["id"] == sent["id"]
            ended = await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_CANCELED"])
            assert state_of(ended) == "TASK_STATE_CANCELED"

    run(main)


def test_cancel_on_a_task_that_has_already_ended_is_not_cancelable() -> None:
    async def main() -> None:
        async with served({"support": talker("all done")}) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "hi"), as_=ALICE))
            await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_COMPLETED"])
            refused = await on.rpc("CancelTask", {"id": sent["id"]}, as_=ALICE)
            assert fault_name(refused) == "TaskNotCancelableError"

    run(main)


def test_a_cancelled_task_cannot_be_cancelled_twice() -> None:
    async def main() -> None:
        async with served(_waiting()) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "hi"), as_=ALICE))
            await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_INPUT_REQUIRED"])
            await on.rpc("CancelTask", {"id": sent["id"]}, as_=ALICE)
            await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_CANCELED"])
            again = await on.rpc("CancelTask", {"id": sent["id"]}, as_=ALICE)
            assert fault_name(again) == "TaskNotCancelableError"

    run(main)


def test_another_principal_cannot_cancel_this_callers_task() -> None:
    async def main() -> None:
        async with served(_waiting()) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "hi"), as_=ALICE))
            await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_INPUT_REQUIRED"])
            theirs = await on.rpc("CancelTask", {"id": sent["id"]}, as_=BOB)
            assert fault_name(theirs) == "TaskNotFoundError"
            # And it is still the caller's to cancel, so the refusal changed nothing.
            mine = await on.rpc("CancelTask", {"id": sent["id"]}, as_=ALICE)
            assert mine.status_code == HTTPStatus.OK

    run(main)


def test_cancel_works_over_the_http_json_binding_too() -> None:
    async def main() -> None:
        async with served(_waiting()) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "hi"), as_=ALICE))
            await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_INPUT_REQUIRED"])
            answered = await on.http("POST", f"/tasks/{sent['id']}:cancel", as_=ALICE, body={})
            assert answered.status_code == HTTPStatus.OK
            assert task(answered)["id"] == sent["id"]
            ended = await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_CANCELED"])
            assert state_of(ended) == "TASK_STATE_CANCELED"

    run(main)
