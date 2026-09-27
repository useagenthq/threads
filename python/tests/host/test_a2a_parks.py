"""A parked run is in-doubt work, and it is visible rather than hidden: it shows WORKING and says
what it waits for. Never FAILED, because we do not know it failed, and never COMPLETED, because we
do not know it worked. A person resolves it through threads, and a partner is never an approver:
no A2A operation decides one of our parks. Mirrors
typescript/packages/host/test/a2a/parks.test.ts."""

import asyncio
import json
from collections.abc import Callable, Coroutine
from http import HTTPStatus
from typing import Final

import pytest
from a2a_agents import actor, say, use
from a2a_kit import ALICE, artifacts_of, fault_name, message, reaches, says, served, state_of, task
from pydantic import JsonValue

from threadsai.host.a2a.keys import a2a_thread_id
from threadsai.host.a2a.state import Slice, task_of
from threadsai.log import EventId
from threadsai.reduce.handlers import to_json

TASK_ID: Final = EventId("01a00000-0000-7000-8000-000000000000")
PARKS: Final = (
    "awaiting_approval",
    "effect_unknown",
    "awaiting_resource",
    "awaiting_member",
    "awaiting_input",
)


def run(main: Callable[[], Coroutine[object, object, None]]) -> None:
    asyncio.run(main())


def test_an_approval_park_shows_working_and_only_an_approver_moves_it_on() -> None:
    async def main() -> None:
        bot = actor([use("refund", {"id": "inv-1"}, "c1"), say("refunded")], [ALICE])
        async with served({"support": bot}) as on:
            sent = task(
                await on.rpc("SendMessage", message("m1", "refund inv-1", contextId="c"), as_=ALICE)
            )
            waiting = await says(on, ALICE, str(sent["id"]), "waiting for approval")
            assert state_of(waiting) == "TASK_STATE_WORKING"
            # A2A offers no way to decide it: the caller's operations are send, get, list, cancel.
            thread = a2a_thread_id(ALICE, "support", "c")
            listed = await on.raw("GET", f"/v1/threads/{thread}/approvals", as_=ALICE)
            pending: JsonValue = json.loads(listed.text)
            assert isinstance(pending, list), pending
            assert len(pending) == 1, pending
            only = pending[0]
            assert isinstance(only, dict), only
            challenge = only["challenge_id"]
            assert isinstance(challenge, str), only
            granted = await on.raw(
                "POST",
                f"/v1/threads/{thread}/approvals/{challenge}",
                as_=ALICE,
                body={"decision": "grant"},
            )
            assert granted.status_code == HTTPStatus.OK
            # Approved through threads, the exposed task carries on and finishes.
            done = await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_COMPLETED"])
            found = artifacts_of(done)[0]
            assert isinstance(found, dict)
            assert found["parts"] == [{"text": "refunded"}]

    run(main)


# The other park reasons cannot be reached from an exposed run with a scripted model: an app tool's
# throw is deliberately an error result the model sees, not uncertainty, and the reasons that do
# park (a provider that went unavailable, a resource, a member) need machinery an exposed agent has
# no way to configure. So the pure function every route answers from is driven directly instead.


def _parked(reason: str) -> JsonValue:
    outcome: JsonValue = {
        "thread_id": "t",
        "branch_id": "b",
        "status": "parked",
        "reason": reason,
        "pending": [],
    }
    view = Slice(task_id=TASK_ID, context_id="c", own=(), outcome=outcome, question=None)
    return to_json(task_of(view).status)


def _said(status: JsonValue) -> JsonValue:
    assert isinstance(status, dict), status
    body = status["message"]
    assert isinstance(body, dict), body
    return body["parts"]


def test_an_uncertain_effect_shows_working_and_asks_for_a_person_not_a_partner() -> None:
    status = _parked("effect_unknown")
    assert isinstance(status, dict)
    assert status["state"] == "TASK_STATE_WORKING"
    assert _said(status) == [{"text": "waiting for a person to resolve an uncertain action"}]


@pytest.mark.parametrize("reason", PARKS)
def test_every_park_reason_shows_working_and_says_what_it_waits_for(reason: str) -> None:
    status = _parked(reason)
    assert isinstance(status, dict)
    assert status["state"] not in ("TASK_STATE_FAILED", "TASK_STATE_COMPLETED")
    parts = _said(status)
    assert isinstance(parts, list)
    first = parts[0]
    assert isinstance(first, dict)
    assert isinstance(first["text"], str)
    assert first["text"] != ""


def test_a_park_reason_with_no_wording_of_its_own_is_reported_as_itself() -> None:
    assert _said(_parked("awaiting_resource")) == [{"text": "awaiting_resource"}]
    assert _said(_parked("awaiting_member")) == [{"text": "awaiting_member"}]


def test_nothing_a_caller_sends_resolves_a_park_of_ours() -> None:
    """An approval park is WORKING, and a WORKING task takes no continuation: the only answer a
    caller can give over A2A is to an ask_user, which is a question, not an authorization."""

    async def main() -> None:
        bot = actor([use("refund", {"id": "inv-1"}, "c1"), say("refunded")], [ALICE])
        async with served({"support": bot}) as on:
            sent = task(
                await on.rpc("SendMessage", message("m1", "refund inv-1", contextId="c"), as_=ALICE)
            )
            waiting = await says(on, ALICE, str(sent["id"]), "waiting for approval")
            body = message("a1", "grant", taskId=waiting["id"], contextId=waiting["contextId"])
            answered = await on.rpc("SendMessage", body, as_=ALICE)
            assert fault_name(answered) == "UnsupportedOperationError"
            still = await says(on, ALICE, str(sent["id"]), "waiting for approval")
            assert state_of(still) == "TASK_STATE_WORKING"

    run(main)
