"""The rows of the design's state table that a run reaches on its own, driven end to end by a
scripted model and asserted through GetTask, which is what a partner actually sees. The park rows
live in
test_a2a_parks.py, which drives the approval flow they belong to. Mirrors
typescript/packages/host/test/a2a/states.test.ts."""

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from http import HTTPStatus

from a2a_agents import asker, reader, structured, talker, worker
from a2a_kit import (
    ALICE,
    ONE,
    artifacts_of,
    message,
    reaches,
    recorded_inputs,
    served,
    state_of,
    task,
    text_of,
)
from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threadsai.host.a2a.config import A2aOptions, default_budget
from threadsai.log import UserInputEvent
from threadsai.reduce.handlers import to_json


def run(main: Callable[[], Coroutine[object, object, None]]) -> None:
    asyncio.run(main())


def _budget_of(recorded: UserInputEvent) -> JsonValue:
    """The budget the run was really decided under, read off its own user_input."""
    found = recorded.data.budget
    assert found is not MISSING, recorded
    return to_json(found)


def _parts(found: Mapping[str, JsonValue]) -> JsonValue:
    artifacts = artifacts_of(found)
    assert len(artifacts) == 1, artifacts
    only = artifacts[0]
    assert isinstance(only, dict), only
    return only["parts"]


def test_an_input_recorded_with_no_model_request_yet_is_submitted() -> None:
    async def main() -> None:
        async with served({"support": talker("hi")}) as on:
            # The answer to SendMessage is read back before the run can have asked the model.
            sent = task(await on.rpc("SendMessage", message("m1", "hello"), as_=ALICE))
            assert state_of(sent) == "TASK_STATE_SUBMITTED"
            status = sent["status"]
            assert isinstance(status, dict)
            assert "message" not in status

    run(main)


def test_a_completed_run_is_completed_with_one_text_artifact() -> None:
    async def main() -> None:
        async with served({"support": talker("the answer")}) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "hello"), as_=ALICE))
            done = await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_COMPLETED"])
            assert _parts(done) == [{"text": "the answer"}]
            # A completed task carries no status message: there is nothing to explain.
            assert text_of(done) == ""

    run(main)


def test_an_agent_with_an_output_schema_completes_with_a_data_part() -> None:
    async def main() -> None:
        async with served({"support": structured("Berlin")}) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "hello"), as_=ALICE))
            done = await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_COMPLETED"])
            assert _parts(done) == [{"data": {"city": "Berlin"}}]

    run(main)


def test_the_artifact_id_is_derived_so_the_same_task_renders_the_same_one() -> None:
    async def main() -> None:
        async with served({"support": talker("hi")}) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "hello"), as_=ALICE))
            first = await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_COMPLETED"])
            again = await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_COMPLETED"])
            assert artifacts_of(again) == artifacts_of(first)

    run(main)


def test_an_ask_user_park_is_input_required_with_the_question_and_its_options() -> None:
    async def main() -> None:
        bot = asker("Which colour?", "done", options=["red", "blue"])
        async with served({"support": bot}) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "hello"), as_=ALICE))
            asked = await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_INPUT_REQUIRED"])
            said = text_of(asked)
            assert "Which colour?" in said
            assert "red" in said
            assert "blue" in said
            # The status message is an agent message carrying the task's own ids.
            status = asked["status"]
            assert isinstance(status, dict)
            body = status["message"]
            assert isinstance(body, dict)
            assert body["role"] == "ROLE_AGENT"
            assert body["taskId"] == asked["id"]
            assert body["contextId"] == asked["contextId"]

    run(main)


def test_a_free_text_question_is_input_required_with_just_the_question() -> None:
    async def main() -> None:
        async with served({"support": asker("What is your order id?", "done")}) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "hello"), as_=ALICE))
            asked = await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_INPUT_REQUIRED"])
            assert text_of(asked) == "What is your order id?"

    run(main)


def test_the_status_message_id_is_derived_so_the_same_state_renders_the_same_message() -> None:
    async def main() -> None:
        async with served({"support": asker("Which colour?", "done")}) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "hello"), as_=ALICE))
            first = await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_INPUT_REQUIRED"])
            again = await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_INPUT_REQUIRED"])
            ids = [_message_id(found) for found in (first, again)]
            assert ids[0] == ids[1]
            # A UUIDv8: derived from logged bytes, never generated.
            assert ids[0][14] == "8"

    run(main)


def _message_id(found: Mapping[str, JsonValue]) -> str:
    status = found["status"]
    assert isinstance(status, dict)
    body = status["message"]
    assert isinstance(body, dict)
    said = body["messageId"]
    assert isinstance(said, str)
    return said


def test_a_run_that_exhausts_its_budget_after_running_is_failed_with_budget_exhausted() -> None:
    async def main() -> None:
        # One model request: the tool call is made, its second request is refused.
        options: A2aOptions = {
            "base_url": "https://host.test",
            "expose": {"support": {"description": "Support.", "budget": {"max_model_requests": 1}}},
        }
        async with served({"support": worker()}, options) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "hello"), as_=ALICE))
            ended = await reaches(
                on,
                ALICE,
                str(sent["id"]),
                ["TASK_STATE_FAILED", "TASK_STATE_COMPLETED", "TASK_STATE_REJECTED"],
            )
            # Exhausted after it ran, so FAILED and not REJECTED, and the code is in the message.
            assert state_of(ended) == "TASK_STATE_FAILED"
            assert text_of(ended).startswith("budget_exhausted: ")
            assert "max_model_requests" in text_of(ended)

    run(main)


def test_an_exposed_run_carries_the_default_budget_when_the_config_states_none() -> None:
    async def main() -> None:
        async with served({"support": talker("hi")}, ONE) as on:
            task(await on.rpc("SendMessage", message("m1", "hello"), as_=ALICE))
            # Read from the log, not the option: this is the budget the run is really decided under.
            recorded = await recorded_inputs(on.store)
            assert len(recorded) == 1
            assert _budget_of(recorded[0]) == to_json(default_budget())

    run(main)


def test_an_exposed_run_carries_the_configured_budget_when_the_config_states_one() -> None:
    async def main() -> None:
        budget: JsonValue = {"max_turns": 3, "max_wall_ms": 5_000}
        options: A2aOptions = {
            "base_url": "https://host.test",
            "expose": {
                "support": {
                    "description": "Support.",
                    "budget": {"max_turns": 3, "max_wall_ms": 5_000},
                }
            },
        }
        async with served({"support": talker("hi")}, options) as on:
            task(await on.rpc("SendMessage", message("m1", "hello"), as_=ALICE))
            recorded = await recorded_inputs(on.store)
            assert _budget_of(recorded[0]) == budget

    run(main)


def test_a_run_refused_before_its_first_model_request_is_rejected_not_failed() -> None:
    async def main() -> None:
        # A budget too small for even one attempt: the run never gets to ask the model anything.
        options: A2aOptions = {
            "base_url": "https://host.test",
            "expose": {
                "support": {
                    "description": "Support.",
                    "budget": {"max_turns": 1, "max_cost_nanos": 1},
                }
            },
        }
        async with served({"support": reader("hi")}, options) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "hello"), as_=ALICE))
            refused = await reaches(
                on,
                ALICE,
                str(sent["id"]),
                ["TASK_STATE_REJECTED", "TASK_STATE_FAILED", "TASK_STATE_COMPLETED"],
            )
            assert state_of(refused) == "TASK_STATE_REJECTED"

    run(main)


def test_a_task_carries_a_timestamp_from_the_log_not_from_the_clock_at_read_time() -> None:
    async def main() -> None:
        async with served({"support": talker("hi")}) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "hello"), as_=ALICE))
            done = await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_COMPLETED"])
            again = await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_COMPLETED"])
            stamps = [_timestamp(found) for found in (done, again)]
            assert stamps[0] == stamps[1]
            assert stamps[0].endswith("Z")

    run(main)


def _timestamp(found: Mapping[str, JsonValue]) -> str:
    status = found["status"]
    assert isinstance(status, dict)
    stamp = status["timestamp"]
    assert isinstance(stamp, str)
    return stamp


def test_one_run_at_a_time_per_context_and_a_settled_task_frees_it() -> None:
    async def main() -> None:
        async with served({"support": asker("Which colour?", "done")}) as on:
            first = task(await on.rpc("SendMessage", message("m1", "hi", contextId="c"), as_=ALICE))
            await reaches(on, ALICE, str(first["id"]), ["TASK_STATE_INPUT_REQUIRED"])
            # A parked task is not "working", so this context can take a new run once it settles.
            second = await on.rpc(
                "SendMessage", message("m2", "hi again", contextId="c"), as_=ALICE
            )
            assert second.status_code == HTTPStatus.OK

    run(main)


def test_an_a2a_run_records_the_message_and_context_it_arrived_as() -> None:
    async def main() -> None:
        async with served({"support": talker("hi")}) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "hi", contextId="c"), as_=ALICE))
            recorded = await recorded_inputs(on.store)
            assert len(recorded) == 1
            arrived = recorded[0].data.a2a
            assert arrived is not MISSING
            assert (arrived.message_id, arrived.context_id) == ("m1", "c")
            assert sent["contextId"] == "c"

    run(main)
