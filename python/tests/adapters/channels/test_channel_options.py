"""The channel factory options lane 15 aligns with TypeScript: WhatsApp replies from the phone
number a message arrived on (no configured phone) through the Graph version it names; Slack's
bot_user_id and GitHub's app_slug say which messages and comments are the channel's own."""

import asyncio
import hashlib
import hmac
import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import JsonValue

from threads.github import github
from threads.host import ChannelAdapter, Ignore, Message, RawRequest
from threads.log import JsonObject
from threads.loop.model import Found, NotFound
from threads.memory.fence import bound
from threads.result import Ok
from threads.secrets import secret
from threads.slack import slack
from threads.whatsapp import whatsapp

SECRET = "shh-signing"  # noqa: S105 - a test signing secret
NOW = 1_790_000_000


@pytest.fixture(autouse=True)
def secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNING", SECRET)
    monkeypatch.setenv("TOKEN", "tok-test-1")
    monkeypatch.setenv("VERIFY", "verify-token")


def hub(body: JsonValue, **headers: str) -> RawRequest:
    raw = json.dumps(body).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return RawRequest({"x-hub-signature-256": signature, **headers}, raw)


type Make = Callable[[httpx.AsyncBaseTransport], ChannelAdapter]


def phone_line(graph_version: str | None = None) -> Make:
    """A WhatsApp channel over a transport; graph_version None leaves the option out."""

    def make(transport: httpx.AsyncBaseTransport) -> ChannelAdapter:
        if graph_version is None:
            return whatsapp(
                app_secret=secret("SIGNING"),
                access_token=secret("TOKEN"),
                verify_token=secret("VERIFY"),
                agent="a",
                transport=transport,
            )
        return whatsapp(
            app_secret=secret("SIGNING"),
            access_token=secret("TOKEN"),
            verify_token=secret("VERIFY"),
            agent="a",
            transport=transport,
            graph_version=graph_version,
        )

    return make


def sent_to(make: Make, op: JsonObject) -> str:
    """The URL perform posted `op` to."""
    urls: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})

    async def allowed() -> bool:
        return True

    async def run() -> None:
        with bound(allowed):
            await make(httpx.MockTransport(record)).perform(op, "b:c", {"access_token": "tok"})

    asyncio.run(run())
    (url,) = urls
    return url


def whatsapp_hook(phone: str) -> RawRequest:
    message: JsonValue = {"id": "wamid.1", "from": "1555", "type": "text", "text": {"body": "hi"}}
    value: JsonValue = {"metadata": {"phone_number_id": phone}, "messages": [message]}
    return hub({"entry": [{"id": "waba", "changes": [{"value": value}]}]})


def test_whatsapp_replies_from_the_phone_number_the_message_arrived_on() -> None:
    channel = phone_line()(httpx.MockTransport(lambda _: httpx.Response(500)))
    for phone in ("PHONE_A", "PHONE_B"):
        verified = channel.verify(whatsapp_hook(phone))
        assert isinstance(verified, Ok)
        op: JsonObject = {"text": "hi", "address": "1555"}
        op["installation_id"] = verified.value.installation_id
        url = sent_to(phone_line(), op)
        assert url == f"https://graph.facebook.com/v21.0/{phone}/messages"


def test_whatsapp_takes_no_configured_phone_and_names_its_graph_version() -> None:
    with pytest.raises(TypeError):
        whatsapp(
            app_secret=secret("SIGNING"),
            access_token=secret("TOKEN"),
            verify_token=secret("VERIFY"),
            phone_number_id="PHONE_A",  # type: ignore[call-arg] - the removed option is refused
            agent="a",
        )
    op: JsonObject = {"text": "hi", "address": "1555", "installation_id": "PHONE_A"}
    url = sent_to(phone_line("v23.0"), op)
    assert url == "https://graph.facebook.com/v23.0/PHONE_A/messages"


def slack_request(user: str) -> RawRequest:
    event: JsonValue = {"type": "message", "user": user, "text": "hi", "channel": "C1"}
    envelope: JsonValue = {
        "type": "event_callback",
        "team_id": "T1",
        "event_id": f"Ev-{user}",
        "event": event,
    }
    body = json.dumps(envelope).encode()
    base = f"v0:{NOW}:".encode() + body
    headers = {
        "x-slack-request-timestamp": str(NOW),
        "x-slack-signature": "v0=" + hmac.new(SECRET.encode(), base, hashlib.sha256).hexdigest(),
        "content-type": "application/json",
    }
    return RawRequest(headers, body)


def test_slack_ignores_the_bot_user_it_is_given() -> None:
    made = slack(
        signing_secret=secret("SIGNING"), bot_token=secret("TOKEN"), agent="a", bot_user_id="UBOT"
    )
    channel = type(made)(
        made.signing_secret, made.bot_token, "a", bot_user_id="UBOT", clock=lambda: NOW
    )
    kinds: list[type[object]] = []
    for user in ("UBOT", "U1"):
        parsed = channel.parse(slack_request(user))
        assert isinstance(parsed, Ok)
        kinds += [type(i) for i in parsed.value]
    assert kinds == [Ignore, Message]


def comment_hook(login: str, kind: str = "User") -> RawRequest:
    body: JsonValue = {
        "action": "created",
        "installation": {"id": 7},
        "sender": {"id": 42, "type": kind, "login": login},
        "repository": {"full_name": "o/r"},
        "issue": {"number": 3},
        "comment": {"body": "hello"},
    }
    return hub(body, **{"x-github-event": "issue_comment"})


def test_github_app_slug_names_the_apps_own_comments() -> None:
    repo = github(
        webhook_secret=secret("SIGNING"), token=secret("TOKEN"), agent="a", app_slug="helper"
    )
    parsed = [repo.parse(comment_hook(login)) for login in ("helper[bot]", "alice")]
    assert [type(p.value[0]) for p in parsed if isinstance(p, Ok)] == [Ignore, Message]

    marker = "<!-- threads:effect_key=b:c -->"
    listing: JsonValue = [
        {"id": 1, "body": f"forged {marker}", "user": {"login": "mallory"}},
        {"id": 2, "body": f"reply {marker}", "user": {"login": "helper[bot]"}},
    ]

    def comments(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=listing)

    async def allowed() -> bool:
        return True

    async def look(slug: str | None) -> object:
        channel = github(
            webhook_secret=secret("SIGNING"),
            token=secret("TOKEN"),
            agent="a",
            app_slug=slug,
            transport=httpx.MockTransport(comments),
        )
        with bound(allowed):
            return await channel.lookup("b:c", {"address": "o/r#3"})

    assert asyncio.run(look("helper")) == Found("2")
    assert asyncio.run(look(None)) == Found("1")
    assert asyncio.run(look("other")) == NotFound()
