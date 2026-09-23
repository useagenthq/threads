"""A channel end to end through the host: a verified, inbox-deduplicated webhook
starts one run; its final response goes out as a host-issued channel_send effect under a
deterministic call id, fenced at the real transport; an uncertain send parks, never re-sent."""

import asyncio
import hashlib
import hmac
import json
import time
from http import HTTPStatus

import httpx
import pytest
from pydantic import JsonValue

from threads import ConfigError, agent, scripted_model, sqlite
from threads.agents.store import Store, open_store, scoped
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
from threads.whatsapp import whatsapp

SECRET = "shh-signing"  # noqa: S105 - a test signing secret
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
    transport: httpx.MockTransport, *requests: RawRequest, settled: type[StoredEvent] | None = None
) -> tuple[list[int], list[StoredEvent]]:
    """The webhooks' answers and the conversation's log once it holds a `settled` event: a
    run goes on after its input is durable, and stopping the host would end it."""
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
        for _ in range(200):
            events = await _events(store)
            if settled is None or any(isinstance(e, settled) for e in events):
                return statuses, events
            await asyncio.sleep(0.01)
    raise AssertionError("the run never settled")


async def _events(store: Store) -> list[StoredEvent]:
    sq = await open_store(scoped(store, "slack:T1"))
    rows = await sq.tables.inbox_rows()
    if not rows:
        return []
    root = await sq.root(rows[0].thread_id)
    if not isinstance(root, Ok):
        return []
    read = await sq.read(root.value, 0)
    assert isinstance(read, Ok)
    return list(read.value.fold.events)


def test_a_redelivered_webhook_runs_once_and_its_reply_is_one_fenced_effect() -> None:
    sent: list[httpx.Request] = []

    def post(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"ok": True, "channel": "C1", "ts": "171.1"})

    first = signed(event("Ev01"))
    statuses, events = asyncio.run(
        deliver(httpx.MockTransport(post), first, first, settled=EffectCommitEvent)
    )
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

    _, events = asyncio.run(
        deliver(httpx.MockTransport(timeout), signed(event("Ev02")), settled=ParkedEvent)
    )
    # One send; then a lookup of the key in the conversation, which can't answer: it parks.
    assert [a.method for a in attempts] == ["POST", "GET"]
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


def test_the_webhook_url_answers_a_subscription_check_only_for_a_channel_that_has_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WA_APP", "wa-app-secret")
    monkeypatch.setenv("WA_TOKEN", "wa-token-1")
    monkeypatch.setenv("WA_VERIFY", "hub-token")
    phone = whatsapp(
        app_secret=secret("WA_APP"),
        access_token=secret("WA_TOKEN"),
        verify_token=secret("WA_VERIFY"),
        phone_number_id="p",
        agent="support",
    )
    talk = slack(
        signing_secret=secret("SLACK_SIGNING_SECRET"),
        bot_token=secret("SLACK_BOT_TOKEN"),
        agent="support",
    )
    bot = agent(model=scripted_model({"responses": []}))
    channels = {"whatsapp": phone, "slack": talk}

    async def main() -> list[tuple[int, str]]:
        served = host(store=sqlite(":memory:"), agents={"support": bot}, channels=channels)
        assert served.channels == ("whatsapp", "slack")
        answers: list[tuple[int, str]] = []
        async with served:
            transport = httpx.ASGITransport(app=served.asgi)
            async with httpx.AsyncClient(transport=transport, base_url="http://h") as client:
                check = {"hub.mode": "subscribe", "hub.challenge": "42"}
                for path, token in (("whatsapp", "hub-token"), ("whatsapp", "no"), ("slack", "x")):
                    params = check | {"hub.verify_token": token}
                    got = await client.get(f"/channels/{path}/events", params=params)
                    answers.append((got.status_code, got.text))
        return answers

    ok, forged, unsupported = asyncio.run(main())
    assert ok == (200, "42")
    assert forged[0] == HTTPStatus.UNAUTHORIZED
    assert '"unverified"' in forged[1]
    assert unsupported[0] == HTTPStatus.NOT_FOUND


def test_ready_refuses_a_channel_whose_secret_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN")
    talk = slack(
        signing_secret=secret("SLACK_SIGNING_SECRET"),
        bot_token=secret("SLACK_BOT_TOKEN"),
        agent="support",
    )
    bot = agent(model=scripted_model({"responses": []}))
    served = host(store=sqlite(":memory:"), agents={"support": bot}, channels={"slack": talk})
    with pytest.raises(ConfigError) as refused:
        asyncio.run(served.ready())
    assert refused.value.code == "missing_secret"
