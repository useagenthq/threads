"""A channel end to end through the host: a verified, inbox-deduplicated webhook
starts one run; its final response goes out as a host-issued channel_send effect under a
deterministic call id, fenced at the real transport; an uncertain send parks, never re-sent."""

import asyncio
import hashlib
import hmac
import json
import time

import httpx
import pytest
from pydantic import JsonValue

from threads import agent, scripted_model, sqlite
from threads.agents.store import open_store, scoped
from threads.host import RawRequest, host
from threads.log import (
    ChannelDeliveryEvent,
    EffectCommitEvent,
    EffectUnknownEvent,
    ParkedEvent,
    ToolCallEvent,
    UserInputEvent,
)
from threads.result import Ok
from threads.secrets import secret
from threads.slack import slack
from threads.store import StoredEvent

SECRET = "shh"  # noqa: S105 - a test signing secret
USAGE: JsonValue = {"input_tokens": 1, "output_tokens": 1}
REPLY: JsonValue = {
    "content": [{"type": "text", "text": "Hi there."}],
    "stop_reason": "end_turn",
    "usage": USAGE,
}


@pytest.fixture(autouse=True)
def secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")


def signed(body: JsonValue) -> RawRequest:
    now = int(time.time())
    raw = json.dumps(body).encode()
    mac = hmac.new(SECRET.encode(), f"v0:{now}:".encode() + raw, hashlib.sha256).hexdigest()
    headers = {"x-slack-request-timestamp": str(now), "x-slack-signature": f"v0={mac}"}
    return RawRequest(headers | {"content-type": "application/json"}, raw)


def event(event_id: str) -> JsonValue:
    message: JsonValue = {"type": "message", "user": "U1", "text": "hello", "channel": "C1"}
    return {"type": "event_callback", "team_id": "T1", "event_id": event_id, "event": message}


async def deliver(
    transport: httpx.MockTransport, *requests: RawRequest
) -> tuple[list[int], list[StoredEvent]]:
    store = sqlite(":memory:")
    channel = slack(
        signing_secret=secret("SLACK_SIGNING_SECRET"),
        bot_token=secret("SLACK_BOT_TOKEN"),
        agent="support",
        transport=transport,
    )
    bot = agent(model=scripted_model({"responses": [REPLY]}))
    statuses: list[int] = []
    async with host(store=store, agents={"support": bot}, channels={"slack": channel}) as served:
        for request in requests:
            answered = await served.receive("slack", request)
            statuses.append(answered.value.status if isinstance(answered, Ok) else 401)
    sq = await open_store(scoped(store, "T1"))
    rows = await sq.tables.inbox_rows()
    if not rows:
        return statuses, []
    root = await sq.root(rows[0].thread_id)
    assert isinstance(root, Ok)
    read = await sq.read(root.value, 0)
    assert isinstance(read, Ok)
    return statuses, list(read.value.fold.events)


def test_a_redelivered_webhook_runs_once_and_its_reply_is_one_fenced_effect() -> None:
    sent: list[httpx.Request] = []

    def post(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"ok": True, "channel": "C1", "ts": "171.1"})

    first = signed(event("Ev01"))
    statuses, events = asyncio.run(deliver(httpx.MockTransport(post), first, first))
    assert statuses == [200, 200]
    (delivery,) = [e for e in events if isinstance(e, ChannelDeliveryEvent)]
    assert delivery.data.item_key == "Ev01#0"
    (entered,) = [e for e in events if isinstance(e, UserInputEvent)]
    assert entered.data.source == "channel"
    assert entered.data.delivery_event_id == delivery.event_id
    assert entered.actor.principal.issuer == "slack:T1"
    (call,) = [e for e in events if isinstance(e, ToolCallEvent)]
    assert call.data.name == "channel_send"
    assert call.data.call_id.startswith("send_")
    assert any(isinstance(e, EffectCommitEvent) for e in events)
    (request,) = sent
    body = json.loads(request.content)
    assert body["channel"] == "C1"
    assert body["text"] == "Hi there."
    assert (
        body["metadata"]["event_payload"]["effect_key"] == f"{call.branch_id}:{call.data.call_id}"
    )
    assert request.headers["authorization"] == "Bearer xoxb-test"


def test_an_unknown_send_outcome_parks_and_is_never_resent() -> None:
    attempts: list[httpx.Request] = []

    def timeout(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ReadTimeout("slow", request=request)

    _, events = asyncio.run(deliver(httpx.MockTransport(timeout), signed(event("Ev02"))))
    assert len(attempts) == 1
    assert any(isinstance(e, EffectUnknownEvent) for e in events)
    parked = [e for e in events if isinstance(e, ParkedEvent)]
    assert [p.data.address.kind for p in parked] == ["effect"]


def test_an_unverified_webhook_is_401_and_stores_nothing() -> None:
    forged = signed(event("Ev03"))
    forged = RawRequest({**forged.headers, "x-slack-signature": "v0=00"}, forged.body)
    statuses, events = asyncio.run(
        deliver(httpx.MockTransport(lambda _r: httpx.Response(500)), forged)
    )
    assert (statuses, events) == ([401], [])
