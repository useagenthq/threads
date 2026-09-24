"""A UI decision that loses a race, and a repeated one (spec/schema/ui/README.md, "Bodies"):
another approver's REST decision landing after the UI route read the log and before it records
is treated as settled (AG-UI streams on with a resume_conflict; the AI SDK's same decision is a
no-op), a second tab repeating an approval gets the stream the first one did, the live hub is
keyed by tenant, and a malformed id on the cursor route is invalid_request."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import TYPE_CHECKING

import httpx
from host.test_http import ALICE, as_, bearer, run, sender
from host.ui_kit import AG_UI, AI_SDK, chat, frames, post, user

from threads import sqlite
from threads._generated.host_api_v1 import AgUiResumeEntry, AiSdkPart
from threads.agents.store import now_ms
from threads.host import Host, host
from threads.host.ui.ag_ui_resume import apply_resume
from threads.host.ui.ai_sdk_decisions import record_parts
from threads.host.ui.common import UiLog, ui_log
from threads.host.ui.hub import LiveHub
from threads.host.ui.key import ui_thread_id
from threads.host.ui.listener import LiveListener
from threads.host.ui.live import Delta
from threads.log import EventId
from threads.result import Ok

if TYPE_CHECKING:
    from pydantic import JsonValue

THREAD = ui_thread_id(ALICE, "support", "chat-1")


@asynccontextmanager
async def hosted(sent: list[str]) -> AsyncGenerator[tuple[Host, httpx.AsyncClient]]:
    served = host(store=sqlite(":memory:"), agents={"support": sender(sent)}, authenticate=bearer)
    async with served:
        transport = httpx.ASGITransport(app=served.asgi)
        async with httpx.AsyncClient(transport=transport, base_url="http://host") as client:
            yield served, client


async def parked(h: Host, client: httpx.AsyncClient) -> tuple[str, UiLog]:
    """The chat parked on its approval, and its log as a UI route read it."""
    body: dict[str, JsonValue] = {
        "threadId": "chat-1",
        "runId": "r1",
        "messages": [{"id": "m1", "role": "user", "content": "Send x"}],
        "tools": [],
    }
    last = frames(await post(client, AG_UI, "alice", body))[-1][1]
    assert isinstance(last, dict)
    outcome = last["outcome"]
    assert isinstance(outcome, dict)
    interrupts = outcome["interrupts"]
    assert isinstance(interrupts, list)
    first = interrupts[0]
    assert isinstance(first, dict)
    log = await ui_log(h, ALICE, THREAD)
    assert log is not None
    return str(first["id"]), log


async def decide(client: httpx.AsyncClient, challenge: str, decision: str) -> None:
    """Another client's decision on the REST route, after the UI route's read."""
    done = await client.post(
        f"/v1/threads/{THREAD}/approvals/{challenge}",
        json={"decision": decision},
        headers=as_("alice"),
    )
    assert done.status_code == HTTPStatus.OK


def grant(challenge: str) -> AgUiResumeEntry:
    return AgUiResumeEntry.model_validate(
        {"interruptId": challenge, "status": "resolved", "payload": {"decision": "grant"}}
    )


def test_ag_ui_a_resume_that_lost_the_race_streams_on_with_a_conflict() -> None:
    async def main() -> None:
        async with hosted([]) as (h, client):
            challenge, stale = await parked(h, client)
            await decide(client, challenge, "deny")
            applied = await apply_resume(
                h, ALICE, stale, [grant(challenge)], now=now_ms(), new_message=False
            )
            conflict: JsonValue = {"interruptId": challenge, "recorded": "denied"}
            assert applied == Ok(
                [{"type": "CUSTOM", "name": "threads.resume_conflict", "value": conflict}]
            )

    run(main)


def test_ag_ui_the_same_decision_as_the_winner_is_a_plain_no_op() -> None:
    async def main() -> None:
        async with hosted([]) as (h, client):
            challenge, stale = await parked(h, client)
            await decide(client, challenge, "grant")
            applied = await apply_resume(
                h, ALICE, stale, [grant(challenge)], now=now_ms(), new_message=False
            )
            assert applied == Ok([])

    run(main)


def test_ai_sdk_the_same_decision_as_the_winner_records_nothing() -> None:
    async def main() -> None:
        async with hosted([]) as (h, client):
            challenge, stale = await parked(h, client)
            await decide(client, challenge, "grant")
            part = AiSdkPart.model_validate(
                {
                    "type": "tool-send",
                    "toolCallId": "call_1",
                    "state": "approval-responded",
                    "approval": {"id": challenge, "approved": True},
                }
            )
            assert await record_parts(h, ALICE, stale, [part]) == Ok([])

    run(main)


def test_a_second_tab_repeating_an_approval_gets_the_first_tabs_stream() -> None:
    async def main() -> None:
        sent: list[str] = []
        async with hosted(sent) as (_, client):
            first = await post(client, AI_SDK, "alice", chat("c", user("m1", "Send x")))
            approval = next(
                d
                for _, d in frames(first)
                if isinstance(d, dict) and d.get("type") == "tool-approval-request"
            )
            assert isinstance(approval, dict)
            part: JsonValue = {
                "type": "tool-send",
                "toolCallId": "call_1",
                "state": "approval-responded",
                "approval": {"id": approval["approvalId"], "approved": True},
            }
            asked: JsonValue = {"id": "a1", "role": "assistant", "parts": [part]}
            tab_a = await post(client, AI_SDK, "alice", chat("c", user("m1", "Send x"), asked))
            tab_b = await post(client, AI_SDK, "alice", chat("c", user("m1", "Send x"), asked))
            assert tab_b.status_code == HTTPStatus.OK
            assert frames(tab_b) == frames(tab_a)
            assert sent == ["x"]

    run(main)


def test_the_live_hub_never_crosses_tenants() -> None:
    async def main() -> None:
        hub = LiveHub()
        mine = LiveListener(hub, "acme", "t-1")
        hub.delta("other", "t-1", Delta(EventId("r"), 0, "theirs"))
        hub.delta("acme", "t-1", Delta(EventId("r"), 0, "mine"))
        assert [d.text for d in mine.take()] == ["mine"]
        mine.stop()

    run(main)


def test_a_malformed_id_on_the_cursor_route_is_invalid_request() -> None:
    async def main() -> None:
        async with hosted([]) as (_, client):
            bad = await client.get("/v1/threads/nope/runs/nope/ui/ai-sdk", headers=as_("alice"))
            assert bad.status_code == HTTPStatus.BAD_REQUEST
            assert bad.json()["error"]["code"] == "invalid_request"

    run(main)
