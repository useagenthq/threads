"""The promise our idempotent-send extension makes: the same messageId from the same authenticated
caller produces one task and **one run**. The receipt lookup happens before any contextId is minted,
so a retry that carries no contextId still answers the original task and the original context.

Every duplicate assertion counts `user_input` rows in the store rather than comparing task ids: a
second run would be a second row, and that is what an equal id could hide. Mirrors
typescript/packages/host/test/a2a/receipts.test.ts."""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Final

from a2a_agents import talker
from a2a_kit import (
    ALICE,
    BOB,
    Agents,
    fault_name,
    message,
    reaches,
    result,
    served,
    state_of,
    task,
    user_inputs,
)
from pydantic import JsonValue

ABSENT: Final = "01a00000-0000-7000-8000-000000000000"
TWO_RUNS: Final = 2


def run(main: Callable[[], Coroutine[object, object, None]]) -> None:
    asyncio.run(main())


def _support() -> Agents:
    return {"support": talker("one", "two", "three", "four")}


def test_the_same_message_id_twice_returns_one_task_and_starts_one_run() -> None:
    async def main() -> None:
        async with served(_support()) as on:
            first = task(await on.rpc("SendMessage", message("dup", "hello"), as_=ALICE))
            second = task(await on.rpc("SendMessage", message("dup", "hello"), as_=ALICE))
            assert second["id"] == first["id"]
            assert await user_inputs(on.store) == 1

    run(main)


def test_a_retry_that_changes_the_context_id_is_another_message_so_it_is_refused() -> None:
    async def main() -> None:
        async with served(_support()) as on:
            sent = message("ctx", "hello", contextId="c-1")
            task(await on.rpc("SendMessage", sent, as_=ALICE))
            # A resend is byte-identical by contract; a body that differs by a field is another
            # message, and answering the first task for it would hide a client bug.
            moved = await on.rpc("SendMessage", message("ctx", "hello", contextId="c-2"), as_=ALICE)
            assert fault_name(moved) == "InvalidParamsError"
            assert await user_inputs(on.store) == 1

    run(main)


def test_an_identical_retry_with_a_context_id_returns_the_same_task_and_context() -> None:
    async def main() -> None:
        async with served(_support()) as on:
            sent = message("ctx", "hello", contextId="c-1")
            first = task(await on.rpc("SendMessage", sent, as_=ALICE))
            assert first["contextId"] == "c-1"
            retry = task(await on.rpc("SendMessage", sent, as_=ALICE))
            assert (retry["id"], retry["contextId"]) == (first["id"], "c-1")
            assert await user_inputs(on.store) == 1

    run(main)


def test_a_retry_with_no_context_id_returns_the_same_task_and_the_same_context() -> None:
    async def main() -> None:
        async with served(_support()) as on:
            first = task(await on.rpc("SendMessage", message("mint", "hello"), as_=ALICE))
            assert isinstance(first["contextId"], str)
            retry = task(await on.rpc("SendMessage", message("mint", "hello"), as_=ALICE))
            # The minted context was recorded in the run's own user_input, so it survives a retry.
            assert retry["contextId"] == first["contextId"]
            assert await user_inputs(on.store) == 1

    run(main)


def test_a_reused_message_id_with_another_body_is_invalid_params() -> None:
    async def main() -> None:
        async with served(_support()) as on:
            task(await on.rpc("SendMessage", message("re", "hello"), as_=ALICE))
            other = await on.rpc("SendMessage", message("re", "something else"), as_=ALICE)
            assert fault_name(other) == "InvalidParamsError"
            assert await user_inputs(on.store) == 1

    run(main)


def test_the_same_message_id_from_two_principals_gives_two_tasks() -> None:
    async def main() -> None:
        async with served(_support()) as on:
            mine = task(await on.rpc("SendMessage", message("shared", "hello"), as_=ALICE))
            theirs = task(await on.rpc("SendMessage", message("shared", "hello"), as_=BOB))
            assert theirs["id"] != mine["id"]
            assert theirs["contextId"] != mine["contextId"]
            assert await user_inputs(on.store) == TWO_RUNS

    run(main)


def test_two_principals_sharing_a_context_id_do_not_share_a_thread() -> None:
    async def main() -> None:
        async with served(_support()) as on:
            mine = task(
                await on.rpc("SendMessage", message("a", "hello", contextId="same"), as_=ALICE)
            )
            theirs = task(
                await on.rpc("SendMessage", message("b", "hello", contextId="same"), as_=BOB)
            )
            # Same contextId, different derived thread: neither reaches the other by guessing one.
            assert theirs["id"] != mine["id"]
            cross = await on.rpc("GetTask", {"id": mine["id"]}, as_=BOB)
            assert fault_name(cross) == "TaskNotFoundError"

    run(main)


def test_a_task_belonging_to_another_principal_is_not_found_on_every_operation() -> None:
    async def main() -> None:
        async with served(_support()) as on:
            mine = task(await on.rpc("SendMessage", message("m", "hello"), as_=ALICE))
            for method in ("GetTask", "CancelTask", "SubscribeToTask"):
                answered = await on.rpc(method, {"id": mine["id"]}, as_=BOB)
                assert fault_name(answered) == "TaskNotFoundError", method

    run(main)


def test_a_task_that_never_existed_answers_exactly_as_another_principals_does() -> None:
    async def main() -> None:
        async with served(_support()) as on:
            mine = task(await on.rpc("SendMessage", message("m", "hello"), as_=ALICE))
            theirs = await on.rpc("GetTask", {"id": mine["id"]}, as_=BOB)
            absent = await on.rpc("GetTask", {"id": ABSENT}, as_=BOB)
            assert fault_name(theirs) == fault_name(absent)

    run(main)


def test_a_message_id_over_255_bytes_is_invalid_params() -> None:
    async def main() -> None:
        async with served(_support()) as on:
            long = await on.rpc("SendMessage", message("x" * 256, "hello"), as_=ALICE)
            assert fault_name(long) == "InvalidParamsError"
            # 255 is accepted, so the refusal is the length and not the shape.
            fine = await on.rpc("SendMessage", message("y" * 255, "hello"), as_=ALICE)
            assert isinstance(task(fine)["id"], str)

    run(main)


def _listed(answered: JsonValue) -> tuple[list[str], int]:
    assert isinstance(answered, dict), answered
    tasks, total = answered["tasks"], answered["totalSize"]
    assert isinstance(tasks, list), answered
    assert isinstance(total, int), answered
    ids: list[str] = []
    for found in tasks:
        assert isinstance(found, dict), found
        assert isinstance(found["id"], str), found
        ids.append(found["id"])
    return (ids, total)


def test_list_tasks_shows_only_the_callers_own_tasks() -> None:
    async def main() -> None:
        async with served(_support()) as on:
            mine = task(await on.rpc("SendMessage", message("m1", "hello"), as_=ALICE))
            task(await on.rpc("SendMessage", message("b1", "hello"), as_=BOB))
            ids, total = _listed(result(await on.rpc("ListTasks", {}, as_=ALICE)))
            assert (ids, total) == ([mine["id"]], 1)
            theirs, _ = _listed(result(await on.rpc("ListTasks", {}, as_=BOB)))
            assert mine["id"] not in theirs

    run(main)


def test_list_tasks_filters_by_context_id() -> None:
    async def main() -> None:
        async with served(_support()) as on:
            here = task(
                await on.rpc("SendMessage", message("m1", "hello", contextId="here"), as_=ALICE)
            )
            there = task(
                await on.rpc("SendMessage", message("m2", "hello", contextId="there"), as_=ALICE)
            )
            filtered, _ = _listed(
                result(await on.rpc("ListTasks", {"contextId": "here"}, as_=ALICE))
            )
            assert filtered == [here["id"]]
            both, _ = _listed(result(await on.rpc("ListTasks", {}, as_=ALICE)))
            assert sorted(both) == sorted([str(here["id"]), str(there["id"])])

    run(main)


def test_list_tasks_pages_and_a_malformed_page_token_is_invalid_params() -> None:
    async def main() -> None:
        async with served(_support()) as on:
            for name in ("p1", "p2", "p3"):
                task(await on.rpc("SendMessage", message(name, "hello", contextId=name), as_=ALICE))
            page = result(await on.rpc("ListTasks", {"pageSize": 2}, as_=ALICE))
            ids, total = _listed(page)
            assert (len(ids), total) == (2, 3)
            assert isinstance(page, dict)
            token = page["nextPageToken"]
            assert isinstance(token, str), page
            assert token != ""
            asked: JsonValue = {"pageSize": 2, "pageToken": token}
            next_page = result(await on.rpc("ListTasks", asked, as_=ALICE))
            assert isinstance(next_page, dict)
            assert len(_listed(next_page)[0]) == 1
            assert next_page["nextPageToken"] == ""
            bad = await on.rpc("ListTasks", {"pageToken": "not-a-token"}, as_=ALICE)
            assert fault_name(bad) == "InvalidParamsError"

    run(main)


def test_list_tasks_shows_a_finished_task_with_the_state_it_ended_in() -> None:
    async def main() -> None:
        async with served(_support()) as on:
            sent = task(await on.rpc("SendMessage", message("done", "hello"), as_=ALICE))
            await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_COMPLETED"])
            listed = result(await on.rpc("ListTasks", {}, as_=ALICE))
            assert isinstance(listed, dict)
            tasks = listed["tasks"]
            assert isinstance(tasks, list), listed
            assert len(tasks) == 1, listed
            only = tasks[0]
            assert isinstance(only, dict)
            assert (only["id"], state_of(only)) == (sent["id"], "TASK_STATE_COMPLETED")

    run(main)
