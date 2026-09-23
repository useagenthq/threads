"""Approval cards over the built-in channels: an approval_requested renders one
op carrying only its challenge id; Slack and WhatsApp post it with buttons whose values carry
only that id, GitHub as text naming the reply; each channel's answer parses to a decision for
that challenge, and free text such as "yes" stays a message."""

import asyncio
import hashlib
import hmac
import json
from collections.abc import Callable
from urllib.parse import quote

import httpx
import pytest
from pydantic import JsonValue

from threads.github import github
from threads.host import ChannelAdapter, Decision, Message, RawRequest, Sent
from threads.log import ApprovalRequestedEvent, JsonObject
from threads.log.jcs import canonicalize
from threads.memory.fence import bound
from threads.result import Ok
from threads.secrets import secret
from threads.slack import slack
from threads.store.lines import parse_log_line
from threads.whatsapp import whatsapp

SECRET = "shh"  # noqa: S105 - a test signing secret
CHALLENGE = "0192c000-0000-7000-8000-00000000000a"
NOW = 1_790_000_000


@pytest.fixture(autouse=True)
def secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNING", SECRET)
    monkeypatch.setenv("TOKEN", "tok")
    monkeypatch.setenv("VERIFY", "v")


def requested() -> ApprovalRequestedEvent:
    line: dict[str, JsonValue] = {
        "seq": 7,
        "event_id": "0192e000-0000-7000-8000-000000000007",
        "thread_id": "0192a000-0000-7000-8000-000000000001",
        "branch_id": "0192b000-0000-7000-8000-000000000001",
        "epoch": 1,
        "type": "approval_requested",
        "type_version": 1,
        "time": 1_790_000_000_007,
        "actor": {"kind": "host"},
        "prev_hash": "0" * 64,
        "critical": True,
        "data": {
            "challenge_id": CHALLENGE,
            "call_id": "call_1",
            "args_hash": "a" * 64,
            "expires_at": 1_790_003_600_000,
        },
    }
    text = canonicalize(line)
    assert isinstance(text, Ok)
    parsed = parse_log_line(text.value)
    assert isinstance(parsed, Ok)
    assert isinstance(parsed.value, ApprovalRequestedEvent)
    return parsed.value


type Make = Callable[[httpx.AsyncBaseTransport], ChannelAdapter]


def posted(make: Make, op: JsonObject, creds: dict[str, str]) -> JsonValue:
    """What perform sent for `op`, through a transport that records the body."""
    seen: list[JsonValue] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "ts": "1.0", "id": 5, "messages": [{}]})

    async def fence() -> bool:
        return True

    async def run() -> object:
        with bound(fence):
            return await make(httpx.MockTransport(record)).perform(op, "b:send_7_0", creds)

    assert isinstance(asyncio.run(run()), Sent)
    (body,) = seen
    return body


def hub(body: JsonValue, **headers: str) -> RawRequest:
    raw = json.dumps(body).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return RawRequest({"x-hub-signature-256": signature, **headers}, raw)


def test_slack_posts_a_card_whose_buttons_carry_only_the_challenge() -> None:
    def make(transport: httpx.AsyncBaseTransport) -> ChannelAdapter:
        return slack(
            signing_secret=secret("SIGNING"),
            bot_token=secret("TOKEN"),
            agent="a",
            transport=transport,
        )

    channel = slack(signing_secret=secret("SIGNING"), bot_token=secret("TOKEN"), agent="a")
    (card,) = channel.render(requested())
    assert card["challenge_id"] == CHALLENGE
    body = posted(make, {**card, "address": "C1:171.1"}, {"bot_token": "tok"})
    assert isinstance(body, dict)
    blocks = body["blocks"]
    assert isinstance(blocks, list)
    values: list[str] = [b["value"] for b in blocks[1]["elements"]]  # type: ignore[index] - JSON
    assert [json.loads(v) for v in values] == [
        {"challenge_id": CHALLENGE, "decision": verdict} for verdict in ("grant", "deny")
    ]
    press: JsonValue = {
        "type": "block_actions",
        "team": {"id": "T1"},
        "user": {"id": "U9"},
        "trigger_id": "tr1",
        "channel": {"id": "C1"},
        "message": {"thread_ts": "171.1"},
        "actions": [{"value": values[0]}],
    }
    form = f"payload={quote(json.dumps(press))}".encode()
    base = f"v0:{NOW}:".encode() + form
    signature = "v0=" + hmac.new(SECRET.encode(), base, hashlib.sha256).hexdigest()
    headers = {
        "x-slack-request-timestamp": str(NOW),
        "x-slack-signature": signature,
        "content-type": "application/x-www-form-urlencoded",
    }
    clocked = type(channel)(channel.signing_secret, channel.bot_token, "a", clock=lambda: NOW)
    parsed = clocked.parse(RawRequest(headers, form))
    assert isinstance(parsed, Ok)
    (decision,) = parsed.value
    assert isinstance(decision, Decision)
    got = (decision.challenge_id, decision.decision, decision.address, decision.principal.subject)
    assert got == (CHALLENGE, "grant", "C1:171.1", "U9")


def test_whatsapp_posts_reply_buttons_and_parses_the_press() -> None:
    def make(transport: httpx.AsyncBaseTransport) -> ChannelAdapter:
        return whatsapp(
            app_secret=secret("SIGNING"),
            access_token=secret("TOKEN"),
            verify_token=secret("VERIFY"),
            phone_number_id="p",
            agent="a",
            transport=transport,
        )

    channel = whatsapp(
        app_secret=secret("SIGNING"),
        access_token=secret("TOKEN"),
        verify_token=secret("VERIFY"),
        phone_number_id="p",
        agent="a",
    )
    (card,) = channel.render(requested())
    body = posted(make, {**card, "address": "1555"}, {"access_token": "tok"})
    assert isinstance(body, dict)
    assert body["type"] == "interactive"
    buttons = body["interactive"]["action"]["buttons"]  # type: ignore[index] - JSON
    assert [b["reply"]["id"] for b in buttons] == [f"approve:{CHALLENGE}", f"deny:{CHALLENGE}"]  # type: ignore[index] - JSON
    reply: JsonValue = {"type": "button_reply", "button_reply": {"id": f"deny:{CHALLENGE}"}}
    messages: list[JsonValue] = [
        {"id": "wamid.1", "from": "1555", "type": "interactive", "interactive": reply},
        {"id": "wamid.2", "from": "1555", "type": "text", "text": {"body": "yes"}},
    ]
    value: JsonValue = {"metadata": {"phone_number_id": "p"}, "messages": messages}
    webhook: JsonValue = {"entry": [{"id": "waba", "changes": [{"value": value}]}]}
    parsed = channel.parse(hub(webhook))
    assert isinstance(parsed, Ok)
    decision, words = parsed.value
    assert isinstance(decision, Decision)
    assert (decision.challenge_id, decision.decision, decision.address) == (
        CHALLENGE,
        "deny",
        "1555",
    )
    assert isinstance(words, Message)


def test_github_asks_for_a_reply_bound_to_the_challenge() -> None:
    channel = github(webhook_secret=secret("SIGNING"), token=secret("TOKEN"), agent="a")
    (card,) = channel.render(requested())
    assert f"/approve {CHALLENGE}" in str(card["text"])
    assert card["challenge_id"] == CHALLENGE

    def comment(text: str) -> RawRequest:
        body: JsonValue = {
            "action": "created",
            "installation": {"id": 42},
            "sender": {"id": 7, "login": "octo", "type": "User"},
            "repository": {"full_name": "o/r"},
            "issue": {"number": 3},
            "comment": {"body": text},
        }
        return hub(body, **{"x-github-event": "issue_comment", "x-github-delivery": "d1"})

    answered = channel.parse(comment(f"/approve {CHALLENGE}"))
    assert isinstance(answered, Ok)
    (decision,) = answered.value
    assert isinstance(decision, Decision)
    assert (decision.challenge_id, decision.decision, decision.address) == (
        CHALLENGE,
        "grant",
        "o/r#3",
    )
    for words in ("yes", f"/approve {CHALLENGE} please", "/approve not-a-challenge"):
        said = channel.parse(comment(words))
        assert isinstance(said, Ok)
        assert [i.kind for i in said.value] == ["message"], words
