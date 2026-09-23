"""The Slack, WhatsApp and GitHub channel adapters: signatures over the raw bytes,
per-item keys, and outbound errors classified by what can have reached the provider. No
network: every send goes to an httpx MockTransport through the fenced client."""

import asyncio
import hashlib
import hmac
import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import JsonValue

from threads.github import github
from threads.host import DeliveryError, Message, RawRequest, Sent
from threads.memory.fence import FenceRefusedError, bound
from threads.result import Err, Ok
from threads.secrets import secret
from threads.slack import slack
from threads.whatsapp import whatsapp

SECRET = "shh"  # noqa: S105 - a test signing secret


@pytest.fixture(autouse=True)
def secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SIGNING", "TOKEN"):
        monkeypatch.setenv(name, SECRET if name == "SIGNING" else "tok")


def slack_request(body: JsonValue, now: int = 1_790_000_000) -> RawRequest:
    raw = json.dumps(body).encode()
    base = f"v0:{now}:".encode() + raw
    signature = "v0=" + hmac.new(SECRET.encode(), base, hashlib.sha256).hexdigest()
    headers = {
        "x-slack-request-timestamp": str(now),
        "x-slack-signature": signature,
        "content-type": "application/json",
    }
    return RawRequest(headers, raw)


def hub_request(body: JsonValue, **headers: str) -> RawRequest:
    raw = json.dumps(body).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return RawRequest({"x-hub-signature-256": signature, **headers}, raw)


def message_event(event_id: str = "Ev01", **event: JsonValue) -> JsonValue:
    base: dict[str, JsonValue] = {"type": "message", "user": "U1", "text": "hi", "channel": "C1"}
    return {"type": "event_callback", "team_id": "T1", "event_id": event_id, "event": base | event}


def fenced[T](call: Callable[[], T]) -> T:
    async def ok() -> bool:
        return True

    with bound(ok):
        return call()


def test_slack_verifies_the_signature_and_keys_its_event() -> None:
    channel = slack(signing_secret=secret("SIGNING"), bot_token=secret("TOKEN"), agent="a")
    channel = type(channel)(
        channel.signing_secret, channel.bot_token, "a", clock=lambda: 1_790_000_000
    )
    request = slack_request(message_event())
    verified = channel.verify(request)
    assert isinstance(verified, Ok)
    assert (verified.value.tenant, verified.value.delivery_id) == ("T1", "Ev01")
    parsed = channel.parse(request)
    assert isinstance(parsed, Ok)
    (item,) = parsed.value
    assert isinstance(item, Message)
    assert (item.item_key, item.address, item.principal.issuer) == ("Ev01#0", "C1", "slack:T1")
    tampered = RawRequest(request.headers, request.body.replace(b"hi", b"yo"))
    assert isinstance(channel.verify(tampered), Err)
    stale = type(channel)(
        channel.signing_secret, channel.bot_token, "a", clock=lambda: 1_890_000_000
    )
    assert isinstance(stale.verify(request), Err)
    own = slack_request(message_event(bot_id="B1"))
    bot_parsed = channel.parse(own)
    assert isinstance(bot_parsed, Ok)
    assert [i.kind for i in bot_parsed.value] == ["ignore"]


def test_whatsapp_keys_each_batched_message_by_its_own_id() -> None:
    channel = whatsapp(
        app_secret=secret("SIGNING"), access_token=secret("TOKEN"), phone_number_id="p", agent="a"
    )
    messages: list[JsonValue] = [
        {"id": f"wamid.{k}", "from": "1555", "type": "text", "text": {"body": k}} for k in "AB"
    ]
    body: JsonValue = {"entry": [{"id": "waba_9", "changes": [{"value": {"messages": messages}}]}]}
    request = hub_request(body)
    verified = channel.verify(request)
    assert isinstance(verified, Ok)
    assert verified.value.tenant == "waba_9"
    parsed = channel.parse(request)
    assert isinstance(parsed, Ok)
    assert [i.item_key for i in parsed.value if isinstance(i, Message)] == ["wamid.A", "wamid.B"]
    assert isinstance(channel.verify(RawRequest({}, request.body)), Err)


def test_github_ignores_bots_and_classifies_send_errors() -> None:
    def answer(status: int) -> httpx.MockTransport:
        return httpx.MockTransport(lambda _r: httpx.Response(status, json={"id": 7}))

    comment: dict[str, JsonValue] = {
        "action": "created",
        "installation": {"id": 42},
        "sender": {"login": "octo", "type": "User"},
        "repository": {"full_name": "o/r"},
        "issue": {"number": 3},
        "comment": {"body": "please"},
    }
    headers = {"x_github_event": "issue_comment", "x_github_delivery": "d1"}
    request = hub_request(comment, **{k.replace("_", "-"): v for k, v in headers.items()})
    channel = github(webhook_secret=secret("SIGNING"), token=secret("TOKEN"), agent="a")
    parsed = channel.parse(request)
    assert isinstance(parsed, Ok)
    (item,) = parsed.value
    assert isinstance(item, Message)
    assert (item.address, item.item_key, item.principal.tenant) == ("o/r#3", "d1#0", "inst_42")
    bot: dict[str, JsonValue] = {**comment, "sender": {"login": "app[bot]", "type": "Bot"}}
    by_bot = hub_request(
        bot,
        **{k.replace("_", "-"): v for k, v in headers.items()},
    )
    bot_parsed = channel.parse(by_bot)
    assert isinstance(bot_parsed, Ok)
    assert [i.kind for i in bot_parsed.value] == ["ignore"]
    op: JsonValue = {"text": "done", "address": "o/r#3"}
    creds = {"token": "tok"}

    def perform(transport: httpx.AsyncBaseTransport) -> object:
        sender = type(channel)(channel.webhook_secret, channel.token, "a", transport=transport)
        return fenced(lambda: asyncio.run(sender.perform(op, "b:c", creds)))

    assert perform(answer(201)) == Sent("7")
    assert perform(answer(429)) == DeliveryError("rate_limited", "definite_not_sent")
    assert perform(answer(422)) == DeliveryError("permanent", "definite_not_sent")
    assert perform(answer(502)) == DeliveryError("transient", "outcome_unknown")

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    def lost(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    connect = perform(httpx.MockTransport(refuse))
    assert connect == DeliveryError("transient", "definite_not_sent")
    assert perform(httpx.MockTransport(lost)) == DeliveryError("transient", "outcome_unknown")


def test_a_send_outside_the_runs_fence_is_refused_before_any_byte() -> None:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    channel = slack(
        signing_secret=secret("SIGNING"),
        bot_token=secret("TOKEN"),
        agent="a",
        transport=httpx.MockTransport(record),
    )
    op: JsonValue = {"text": "x", "address": "C1"}
    with pytest.raises(FenceRefusedError):
        asyncio.run(channel.perform(op, "b:c", {"bot_token": "tok"}))
    assert seen == []
