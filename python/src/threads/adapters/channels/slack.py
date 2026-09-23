"""`slack()` (extra `slack`, ): Slack's Events API and interactivity.

Inbound requests are verified with the official SDK's signing-secret check (slack_sdk), over
the raw bytes and the request timestamp. A message event becomes one item keyed
`<event_id>#0`; the bot's own messages and edits are ignored, and an `app_mention` (Slack sends a
mention as a `message` too) is not a second input. The tenant is `slack:<team_id>`; on Enterprise
Grid the installation is `<enterprise_id>/<team_id>`. A button's value is the JSON
`{"challenge_id", "decision"}` and nothing else, so it approves nothing without the host's check;
each press is keyed `<trigger_id>#<index>`. These are the TS adapter's formats.
Outbound `chat.postMessage` carries the effect key in the message metadata, which lookup finds in
the conversation's recent history; only recent history is read, so a miss parks the send.
"""

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal
from urllib.parse import parse_qs

import httpx
from pydantic import Field, JsonValue, ValidationError
from slack_sdk.signature import Clock, SignatureVerifier

from threads.adapters.channels.common import (
    Loose,
    body_of,
    challenge_of,
    client,
    refused,
    render_ops,
    send,
    unverified,
)
from threads.host.channel import (
    ChannelCapabilities,
    Decision,
    DeliveryError,
    DeliveryOutcome,
    Ignore,
    Inbound,
    Message,
    RawRequest,
    RawResponse,
    Sent,
    VerifiedDelivery,
)
from threads.log import Event, JsonObject, ParseError, Principal
from threads.loop.model import Found, LookupResult, LookupUnknown, NotFound
from threads.memory.fence import check
from threads.result import Err, Ok
from threads.secrets import Secret, resolve

API: Final = "https://slack.com/api"
_LIMITS: Final = {"message_bytes": 40_000}


class _Event(Loose):
    type: str
    user: str | None = None
    text: str = ""
    channel: str | None = None
    bot_id: str | None = None
    subtype: str | None = None
    thread_ts: str | None = None


class _Envelope(Loose):
    type: str
    team_id: str | None = None
    enterprise_id: str | None = None
    event_id: str | None = None
    challenge: str | None = None
    event: _Event | None = None


class _Id(Loose):
    id: str


class _Team(Loose):
    id: str
    enterprise_id: str | None = None


class _Action(Loose):
    value: str = ""


class _Button(Loose):
    challenge_id: str
    decision: Literal["grant", "deny"]


class _Card(Loose):
    thread_ts: str | None = None


class _Interaction(Loose):
    type: str
    team: _Team
    user: _Id
    trigger_id: str
    channel: _Id | None = None
    message: _Card = Field(default_factory=_Card)
    actions: tuple[_Action, ...] = ()


class _SlackClock(Clock):
    def __init__(self, now: Callable[[], float]) -> None:
        self._now = now

    def now(self) -> float:
        return self._now()


class _Metadata(Loose):
    event_payload: Mapping[str, JsonValue] = Field(default_factory=dict[str, JsonValue])


class _Posted(Loose):
    ts: str = ""
    metadata: _Metadata | None = None


class _History(Loose):
    ok: bool = False
    messages: tuple[_Posted, ...] = ()


type Payload = _Envelope | _Interaction


@dataclass(frozen=True, slots=True)
class SlackChannel:
    signing_secret: Secret
    bot_token: Secret
    agent: str
    api: str = API
    transport: httpx.AsyncBaseTransport | None = None
    clock: Callable[[], float] = time.time
    capabilities: ChannelCapabilities = field(
        default_factory=lambda: ChannelCapabilities("nonfinal", True, True, True, True)
    )
    limits: Mapping[str, int] = field(default_factory=lambda: dict(_LIMITS))

    @property
    def secrets(self) -> Mapping[str, Secret]:
        return {"bot_token": self.bot_token}

    def verify(self, raw: RawRequest) -> Ok[VerifiedDelivery] | Err[ParseError]:
        timestamp = raw.headers.get("x-slack-request-timestamp")
        signature = raw.headers.get("x-slack-signature")
        checker = SignatureVerifier(resolve(self.signing_secret), _SlackClock(self.clock))
        try:
            valid = checker.is_valid(raw.body, timestamp, signature)
        except ValueError:
            valid = False
        if not valid:
            return unverified("the Slack signature does not match")
        payload = _payload(raw)
        if payload is None:
            return unverified("not a Slack event or interaction")
        match payload:
            case _Envelope(type="url_verification"):
                return Ok(VerifiedDelivery("", "", "url_verification"))
            case _Envelope(team_id=str(team), enterprise_id=grid, event_id=str(event_id)):
                return Ok(VerifiedDelivery(f"slack:{team}", _installation(team, grid), event_id))
            case _Interaction(team=team, trigger_id=trigger):
                installation = _installation(team.id, team.enterprise_id)
                return Ok(VerifiedDelivery(f"slack:{team.id}", installation, trigger))
            case _:
                return unverified("the payload names no workspace")

    def parse(self, raw: RawRequest) -> Ok[Sequence[Inbound]] | Err[ParseError]:
        payload = _payload(raw)
        verified = self.verify(raw)
        if payload is None or isinstance(verified, Err):
            return Err(ParseError("invalid", "not a verified Slack payload"))
        return Ok(_items(payload, verified.value))

    def ack(self, raw: RawRequest) -> RawResponse:
        payload = _payload(raw)
        if isinstance(payload, _Envelope) and payload.challenge is not None:
            body = payload.challenge.encode()
            return RawResponse(200, {"content-type": "text/plain"}, body)
        return RawResponse(200, {}, b"")

    def render(self, event: Event) -> Sequence[JsonObject]:
        return render_ops(event)

    async def perform(
        self, op: JsonObject, effect_key: str, credentials: Mapping[str, str]
    ) -> DeliveryOutcome:
        channel, _, thread_ts = str(op["address"]).partition(":")
        body: dict[str, JsonValue] = {
            "channel": channel,
            "text": op["text"],
            "metadata": {"event_type": "threads_effect", "event_payload": {"effect_key": effect_key}},
        }
        if thread_ts:
            body["thread_ts"] = thread_ts
        challenge = challenge_of(op)
        if challenge is not None:
            body["blocks"] = _card(str(op["text"]), challenge)
        headers = {"authorization": f"Bearer {credentials['bot_token']}"}
        async with client(self.transport) as http:
            response = await send(http, f"{self.api}/chat.postMessage", headers, body)
        if isinstance(response, DeliveryError):
            return response
        failed = refused(response)
        if failed is not None:
            return failed
        answer = body_of(response)
        if answer.get("ok") is not True:
            # Slack answered that it did not post: rejected, nothing sent.
            kind = "rate_limited" if answer.get("error") == "ratelimited" else "permanent"
            return DeliveryError(kind, "definite_not_sent")
        return Sent(f"{answer.get('channel', channel)}:{answer.get('ts', '')}")

    async def lookup(self, effect_key: str, op: JsonObject) -> LookupResult[str]:
        """The conversation's recent message whose metadata carries the key. Only recent
        history is read, so the channel declares nonfinal and a miss parks the send."""
        await check()
        channel, _, thread_ts = str(op["address"]).partition(":")
        method = "conversations.replies" if thread_ts else "conversations.history"
        query = {"channel": channel, "limit": "200", "include_all_metadata": "true"}
        if thread_ts:
            query["ts"] = thread_ts
        headers = {"authorization": f"Bearer {resolve(self.bot_token)}"}
        try:
            async with client(self.transport) as http:
                response = await http.get(f"{self.api}/{method}", headers=headers, params=query)
        except httpx.HTTPError as error:
            return LookupUnknown(f"history failed: {type(error).__name__}")
        history = _History.model_validate_json(response.content or b"{}")
        if refused(response) is not None or not history.ok:
            return LookupUnknown(f"{method} answered {response.status_code}")
        for posted in history.messages:
            if (
                posted.metadata is not None
                and posted.metadata.event_payload.get("effect_key") == effect_key
            ):
                return Found(f"{channel}:{posted.ts}")
        return NotFound()


def _card(text: str, challenge: str) -> JsonValue:
    """The approval card: its text and two buttons whose values carry only the challenge id."""
    buttons: list[JsonValue] = [
        {
            "type": "button",
            "action_id": f"threads_{verdict}",
            "text": {"type": "plain_text", "text": label},
            "style": style,
            "value": json.dumps({"challenge_id": challenge, "decision": verdict}),
        }
        for verdict, label, style in (("grant", "Approve", "primary"), ("deny", "Deny", "danger"))
    ]
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions", "elements": buttons},
    ]


def _payload(raw: RawRequest) -> Payload | None:
    """Events API requests are JSON; interactivity is a form with a `payload` field."""
    try:
        if raw.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
            form = parse_qs(raw.body.decode("utf-8"))
            return _Interaction.model_validate_json(form.get("payload", [""])[0])
        return _Envelope.model_validate_json(raw.body)
    except (ValidationError, UnicodeDecodeError):
        return None


def _installation(team: str, enterprise: str | None) -> str:
    """Enterprise Grid: the org qualifies the workspace, so E1/T1 and E2/T1 never collide."""
    return f"{enterprise}/{team}" if enterprise else team


def _items(payload: Payload, delivery: VerifiedDelivery) -> Sequence[Inbound]:
    if isinstance(payload, _Interaction):
        return tuple(_decision(payload, delivery, i) for i in range(len(payload.actions)))
    return (_message(payload, delivery),)


def _decision(press: _Interaction, delivery: VerifiedDelivery, index: int) -> Inbound:
    try:
        answer = _Button.model_validate_json(press.actions[index].value)
    except ValidationError:
        return Ignore(kind="ignore")
    if press.channel is None:
        return Ignore(kind="ignore")
    # The card was posted in the conversation's thread: the press answers from there.
    channel, thread_ts = press.channel.id, press.message.thread_ts
    return Decision(
        kind="decision",
        principal=_principal(press.team.id, delivery.tenant, press.user.id),
        address=f"{channel}:{thread_ts}" if thread_ts else channel,
        item_key=f"{delivery.delivery_id}#{index}",
        challenge_id=answer.challenge_id,
        decision=answer.decision,
    )


def _message(payload: _Envelope, delivery: VerifiedDelivery) -> Inbound:
    event = payload.event
    if event is None or event.type != "message" or payload.team_id is None:
        return Ignore(kind="ignore")
    if event.bot_id is not None or event.subtype is not None or not event.user:
        # The bot's own messages, edits and joins are not input.
        return Ignore(kind="ignore")
    if not event.channel or not event.text:
        return Ignore(kind="ignore")
    address = f"{event.channel}:{event.thread_ts}" if event.thread_ts else event.channel
    return Message(
        kind="message",
        principal=_principal(payload.team_id, delivery.tenant, event.user),
        address=address,
        item_key=f"{delivery.delivery_id}#0",
        content=event.text,
    )


def _principal(team: str, tenant: str, user: str) -> Principal:
    return Principal(issuer=f"slack:{team}", tenant=tenant, subject=user)


def slack(
    *,
    signing_secret: Secret,
    bot_token: Secret,
    agent: str,
    api: str = API,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SlackChannel:
    """A Slack channel for host(channels=...). Secrets are resolved on the host at use."""
    return SlackChannel(signing_secret, bot_token, agent, api, transport)
