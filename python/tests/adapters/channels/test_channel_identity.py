"""The same signed request names the same tenant, installation, principal and item key in both
languages (the TS adapters' formats), and a replayed body is the same inbox item."""

import hashlib
import hmac
import json
from urllib.parse import quote

import pytest
from pydantic import JsonValue

from threads.github import github
from threads.host import Decision, Message, RawRequest
from threads.result import Err, Ok
from threads.secrets import secret
from threads.slack import slack
from threads.whatsapp import whatsapp

SECRET = "shh-signing"  # noqa: S105 - a test signing secret
NOW = 1_790_000_000
CHALLENGE = "0192c000-0000-7000-8000-00000000000a"


@pytest.fixture(autouse=True)
def secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNING", SECRET)
    monkeypatch.setenv("TOKEN", "tok-test-1")
    monkeypatch.setenv("VERIFY", "verify-token")


def slack_signed(body: bytes, form: bool = False) -> RawRequest:
    base = f"v0:{NOW}:".encode() + body
    headers = {
        "x-slack-request-timestamp": str(NOW),
        "x-slack-signature": "v0=" + hmac.new(SECRET.encode(), base, hashlib.sha256).hexdigest(),
        "content-type": "application/x-www-form-urlencoded" if form else "application/json",
    }
    return RawRequest(headers, body)


def hub(body: JsonValue, **headers: str) -> RawRequest:
    raw = json.dumps(body).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return RawRequest({"x-hub-signature-256": signature, **headers}, raw)


def test_slack_tenant_is_prefixed_and_enterprise_qualifies_the_installation() -> None:
    channel = slack(signing_secret=secret("SIGNING"), bot_token=secret("TOKEN"), agent="a")
    channel = type(channel)(channel.signing_secret, channel.bot_token, "a", clock=lambda: NOW)
    event: dict[str, JsonValue] = {"type": "message", "user": "U1", "text": "hi", "channel": "C1"}
    envelope: dict[str, JsonValue] = {
        "type": "event_callback",
        "team_id": "T1",
        "enterprise_id": "E1",
        "event_id": "Ev1",
        "event": event,
    }
    request = slack_signed(json.dumps(envelope).encode())
    verified = channel.verify(request)
    assert isinstance(verified, Ok)
    assert (verified.value.tenant, verified.value.installation_id) == ("slack:T1", "E1/T1")
    parsed = channel.parse(request)
    assert isinstance(parsed, Ok)
    (item,) = parsed.value
    assert isinstance(item, Message)
    assert (item.principal.issuer, item.principal.tenant) == ("slack:T1", "slack:T1")


def test_slack_answers_a_mention_once() -> None:
    """A mention arrives as `message` and as `app_mention`; only `message` is input."""
    channel = slack(signing_secret=secret("SIGNING"), bot_token=secret("TOKEN"), agent="a")
    channel = type(channel)(channel.signing_secret, channel.bot_token, "a", clock=lambda: NOW)
    event: dict[str, JsonValue] = {"type": "app_mention", "user": "U1", "text": "<@B> hi"}
    envelope: JsonValue = {
        "type": "event_callback",
        "team_id": "T1",
        "event_id": "Ev2",
        "event": event | {"channel": "C1"},
    }
    parsed = channel.parse(slack_signed(json.dumps(envelope).encode()))
    assert isinstance(parsed, Ok)
    assert [i.kind for i in parsed.value] == ["ignore"]


def test_slack_button_values_are_json_and_the_press_keeps_its_thread() -> None:
    channel = slack(signing_secret=secret("SIGNING"), bot_token=secret("TOKEN"), agent="a")
    channel = type(channel)(channel.signing_secret, channel.bot_token, "a", clock=lambda: NOW)
    value = json.dumps({"challenge_id": CHALLENGE, "decision": "grant"})
    actions: list[JsonValue] = [{"value": value}, {"value": f"grant:{CHALLENGE}"}]
    press: JsonValue = {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": "U9"},
        "trigger_id": "tr1",
        "channel": {"id": "C1"},
        "message": {"thread_ts": "1700.1"},
        "actions": actions,
    }
    request = slack_signed(f"payload={quote(json.dumps(press))}".encode(), form=True)
    verified = channel.verify(request)
    assert isinstance(verified, Ok)
    assert verified.value.delivery_id == "tr1"
    parsed = channel.parse(request)
    assert isinstance(parsed, Ok)
    decision, old = parsed.value
    assert isinstance(decision, Decision)
    got = (decision.address, decision.item_key, decision.challenge_id, decision.principal.tenant)
    assert got == ("C1:1700.1", "tr1#0", CHALLENGE, "slack:T1")
    assert old.kind == "ignore"


def github_comment(**extra: JsonValue) -> dict[str, JsonValue]:
    return {
        "action": "created",
        "installation": {"id": 42},
        "sender": {"id": 7, "login": "octo", "type": "User"},
        "repository": {"full_name": "o/r"},
        "issue": {"number": 3},
        "comment": {"id": 900, "body": "please"},
        **extra,
    }


def test_github_keys_the_item_by_the_signed_body_not_the_delivery_header() -> None:
    channel = github(webhook_secret=secret("SIGNING"), token=secret("TOKEN"), agent="a")
    keys: list[str] = []
    for delivery in ("d1", "d2"):
        request = hub(github_comment(), **{"x-github-event": "issue_comment"})
        request = RawRequest({**request.headers, "x-github-delivery": delivery}, request.body)
        verified = channel.verify(request)
        assert isinstance(verified, Ok)
        assert (verified.value.tenant, verified.value.installation_id) == ("github:42", "42")
        parsed = channel.parse(request)
        assert isinstance(parsed, Ok)
        (item,) = parsed.value
        assert isinstance(item, Message)
        assert (item.principal.issuer, item.principal.subject) == ("github:42", "7")
        keys.append(item.item_key)
    assert keys[0] == keys[1]


def whatsapp_entry(waba: str, phone: str, wamid: str) -> JsonValue:
    message: JsonValue = {"id": wamid, "from": "1555", "type": "text", "text": {"body": "hi"}}
    value: JsonValue = {"metadata": {"phone_number_id": phone}, "messages": [message]}
    return {"id": waba, "changes": [{"value": value}]}


def test_whatsapp_tenant_is_the_phone_number() -> None:
    channel = whatsapp(
        app_secret=secret("SIGNING"),
        access_token=secret("TOKEN"),
        verify_token=secret("VERIFY"),
        phone_number_id="P1",
        agent="a",
    )
    entries = [whatsapp_entry("WABA1", "P1", "wamid.1"), whatsapp_entry("WABA2", "P1", "wamid.2")]
    request = hub({"object": "whatsapp_business_account", "entry": entries})
    verified = channel.verify(request)
    assert isinstance(verified, Ok)
    assert (verified.value.tenant, verified.value.installation_id) == ("whatsapp:P1", "P1")
    parsed = channel.parse(request)
    assert isinstance(parsed, Ok)
    who = {(i.principal.issuer, i.principal.tenant) for i in parsed.value if isinstance(i, Message)}
    assert who == {("whatsapp:P1", "whatsapp:P1")}
    two = [whatsapp_entry("WABA1", "P1", "wamid.1"), whatsapp_entry("WABA1", "P2", "wamid.2")]
    refused = channel.verify(hub({"object": "whatsapp_business_account", "entry": two}))
    assert isinstance(refused, Err)
    assert refused.error.code == "unverified"
