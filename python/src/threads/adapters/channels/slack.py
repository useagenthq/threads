"""`slack()` (extra `slack`, ): Slack's Events API and interactivity.

Inbound requests are verified with the official SDK's signing-secret check (slack_sdk), over
the raw bytes and the request timestamp. A message event becomes one item keyed
`<event_id>#0`; the bot's own messages and edits are ignored. A button press carries only a
challenge id (`grant:<id>` / `deny:<id>`), so it approves nothing without the host's check.
Outbound `chat.postMessage` carries the effect key in the message metadata. Slack has no lookup
by that key, so an uncertain send parks.
"""

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import parse_qs

import httpx
from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError
from slack_sdk.signature import Clock, SignatureVerifier

from threads.adapters.channels.common import body_of, client, final_text, refused, send, unverified
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
from threads.loop.model import LookupResult, LookupUnknown
from threads.result import Err, Ok
from threads.secrets import Secret, resolve

API: Final = "https://slack.com/api"
_LIMITS: Final = {"message_bytes": 40_000}


class _Loose(BaseModel):
    """Provider payloads carry more than we read; only the fields below are checked."""

    model_config = ConfigDict(extra="ignore", frozen=True)


class _Event(_Loose):
    type: str
    user: str | None = None
    text: str = ""
    channel: str | None = None
    bot_id: str | None = None
    subtype: str | None = None
    thread_ts: str | None = None


class _Envelope(_Loose):
    type: str
    team_id: str | None = None
    event_id: str | None = None
    challenge: str | None = None
    event: _Event | None = None


class _Id(_Loose):
    id: str


class _Action(_Loose):
    value: str = ""
    action_ts: str = ""


class _Interaction(_Loose):
    type: str
    team: _Id
    user: _Id
    channel: _Id | None = None
    actions: tuple[_Action, ...] = ()


class _SlackClock(Clock):
    def __init__(self, now: Callable[[], float]) -> None:
        self._now = now

    def now(self) -> float:
        return self._now()


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
        default_factory=lambda: ChannelCapabilities("none", True, True, True, True)
    )
    limits: Mapping[str, int] = field(default_factory=lambda: dict(_LIMITS))

    @property
    def credentials(self) -> Mapping[str, Secret]:
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
            case _Envelope(team_id=str(team), event_id=str(event_id)):
                return Ok(VerifiedDelivery(team, team, event_id))
            case _Interaction(team=team, actions=(first, *_)):
                return Ok(VerifiedDelivery(team.id, team.id, f"action:{first.action_ts}"))
            case _:
                return unverified("the payload names no workspace")

    def parse(self, raw: RawRequest) -> Ok[Sequence[Inbound]] | Err[ParseError]:
        payload = _payload(raw)
        verified = self.verify(raw)
        if payload is None or isinstance(verified, Err):
            return Err(ParseError("invalid", "not a verified Slack payload"))
        return Ok((_item(payload, verified.value),))

    def ack(self, raw: RawRequest) -> RawResponse:
        payload = _payload(raw)
        if isinstance(payload, _Envelope) and payload.challenge is not None:
            body = payload.challenge.encode()
            return RawResponse(200, {"content-type": "text/plain"}, body)
        return RawResponse(200, {}, b"")

    def render(self, event: Event) -> Sequence[JsonObject]:
        text = final_text(event)
        return () if text is None else ({"text": text},)

    async def perform(
        self, op: JsonObject, effect_key: str, credentials: Mapping[str, str]
    ) -> DeliveryOutcome:
        channel, _, thread_ts = str(op["address"]).partition(":")
        body: dict[str, JsonValue] = {
            "channel": channel,
            "text": op["text"],
            "metadata": {"event_type": "threads_send", "event_payload": {"effect_key": effect_key}},
        }
        if thread_ts:
            body["thread_ts"] = thread_ts
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

    async def lookup(self, effect_key: str) -> LookupResult[str]:
        return LookupUnknown("Slack has no lookup by effect key")


def _payload(raw: RawRequest) -> Payload | None:
    """Events API requests are JSON; interactivity is a form with a `payload` field."""
    try:
        if raw.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
            form = parse_qs(raw.body.decode("utf-8"))
            return _Interaction.model_validate_json(form.get("payload", [""])[0])
        return _Envelope.model_validate_json(raw.body)
    except (ValidationError, UnicodeDecodeError):
        return None


def _item(payload: Payload, delivery: VerifiedDelivery) -> Inbound:
    team = delivery.tenant
    key = f"{delivery.delivery_id}#0"
    if isinstance(payload, _Interaction):
        verdict, _, challenge = payload.actions[0].value.partition(":")
        if verdict not in ("grant", "deny") or not challenge or payload.channel is None:
            return Ignore(kind="ignore")
        return Decision(
            kind="decision",
            principal=_principal(team, payload.user.id),
            address=payload.channel.id,
            item_key=key,
            challenge_id=challenge,
            decision="grant" if verdict == "grant" else "deny",
        )
    event = payload.event
    if event is None or event.type not in ("message", "app_mention"):
        return Ignore(kind="ignore")
    if event.bot_id is not None or event.subtype is not None or not event.user:
        # The bot's own messages, edits and joins are not input.
        return Ignore(kind="ignore")
    if not event.channel or not event.text:
        return Ignore(kind="ignore")
    address = event.channel if event.thread_ts is None else f"{event.channel}:{event.thread_ts}"
    return Message(
        kind="message",
        principal=_principal(team, event.user),
        address=address,
        item_key=key,
        content=event.text,
    )


def _principal(team: str, user: str) -> Principal:
    return Principal(issuer=f"slack:{team}", tenant=team, subject=user)


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
