"""`whatsapp()` (extra `whatsapp`, ): the WhatsApp Cloud API.

A webhook is verified by `X-Hub-Signature-256` over the raw bytes with the app secret, and the
URL's GET subscription check by the verify token, compared in constant time. The business phone
number is the installation and the tenant is `whatsapp:<phone_number_id>` (TS's formats); a
webhook that speaks for two phone numbers is refused rather than filed under one. One webhook may batch several messages, each keyed by
its own `wamid`, so none is dropped as a duplicate of a neighbor. Outbound sends carry the
effect key as `biz_opaque_callback_data`. The Cloud API has no lookup by that key, so an
uncertain send parks. Meta publishes no official Python SDK; the REST
API is called through the fenced httpx client.
"""

import hashlib
import hmac
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal
from uuid import UUID

import httpx
from pydantic import Field, JsonValue, ValidationError

from threads.adapters.channels.common import (
    Loose,
    body_of,
    challenge_of,
    client,
    hub_signature,
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
from threads.loop.model import LookupResult, LookupUnknown
from threads.result import Err, Ok
from threads.secrets import Secret, resolve

API: Final = "https://graph.facebook.com/v21.0"


class _Text(Loose):
    body: str


class _Reply(Loose):
    id: str


class _Interactive(Loose):
    type: str
    button_reply: _Reply | None = None


class _Message(Loose):
    id: str
    sender: str = Field(alias="from")
    type: str
    text: _Text | None = None
    interactive: _Interactive | None = None


class _Metadata(Loose):
    phone_number_id: str = Field(min_length=1)


class _Value(Loose):
    metadata: _Metadata
    messages: tuple[_Message, ...] = ()


class _Change(Loose):
    value: _Value


class _Entry(Loose):
    changes: tuple[_Change, ...] = ()


class _Button(Loose):
    challenge_id: UUID
    decision: Literal["grant", "deny"]


class _Webhook(Loose):
    entry: tuple[_Entry, ...]


@dataclass(frozen=True, slots=True)
class WhatsAppChannel:
    app_secret: Secret
    access_token: Secret
    verify_token: Secret
    phone_number_id: str
    agent: str
    api: str = API
    transport: httpx.AsyncBaseTransport | None = None
    capabilities: ChannelCapabilities = field(
        default_factory=lambda: ChannelCapabilities("none", True, False, True, True)
    )
    limits: Mapping[str, int] = field(default_factory=lambda: {"message_bytes": 4096})

    @property
    def secrets(self) -> Mapping[str, Secret]:
        return {"access_token": self.access_token}

    def verify(self, raw: RawRequest) -> Ok[VerifiedDelivery] | Err[ParseError]:
        header = raw.headers.get("x-hub-signature-256")
        if not hub_signature(resolve(self.app_secret), raw.body, header):
            return unverified("the WhatsApp signature does not match")
        hook = _webhook(raw)
        if hook is None:
            return unverified("not a WhatsApp webhook")
        phones = {c.value.metadata.phone_number_id for e in hook.entry for c in e.changes}
        if len(phones) != 1:
            return unverified("the webhook must speak for exactly one phone number")
        (phone,) = phones
        # Meta sends no delivery id: the signed bytes identify the delivery.
        delivery = hashlib.sha256(raw.body).hexdigest()
        return Ok(VerifiedDelivery(f"whatsapp:{phone}", phone, delivery))

    def challenge(self, query: Mapping[str, str]) -> Ok[RawResponse] | Err[ParseError]:
        """Meta's subscription check: hub.verify_token must equal the verify token (constant
        time), then hub.challenge is echoed."""
        token = query.get("hub.verify_token", "")
        expected = resolve(self.verify_token)
        if query.get("hub.mode") != "subscribe" or not hmac.compare_digest(token, expected):
            return unverified("the WhatsApp verify token does not match")
        body = query.get("hub.challenge", "").encode()
        return Ok(RawResponse(200, {"content-type": "text/plain"}, body))

    def parse(self, raw: RawRequest) -> Ok[Sequence[Inbound]] | Err[ParseError]:
        hook = _webhook(raw)
        if hook is None:
            return Err(ParseError("invalid", "not a WhatsApp webhook"))
        items: list[Inbound] = []
        for entry in hook.entry:
            for change in entry.changes:
                phone = change.value.metadata.phone_number_id
                items.extend(_item(phone, m) for m in change.value.messages)
        return Ok(items)

    def ack(self, raw: RawRequest) -> RawResponse:
        return RawResponse(200, {}, b"")

    def render(self, event: Event) -> Sequence[JsonObject]:
        return render_ops(event)

    async def perform(
        self, op: JsonObject, effect_key: str, credentials: Mapping[str, str]
    ) -> DeliveryOutcome:
        body: dict[str, JsonValue] = {
            "messaging_product": "whatsapp",
            "to": op["address"],
            "type": "text",
            "text": {"body": op["text"]},
            "biz_opaque_callback_data": effect_key,
        }
        challenge = challenge_of(op)
        if challenge is not None:
            del body["text"]
            body |= {"type": "interactive", "interactive": _card(str(op["text"]), challenge)}
        headers = {"authorization": f"Bearer {credentials['access_token']}"}
        url = f"{self.api}/{self.phone_number_id}/messages"
        async with client(self.transport) as http:
            response = await send(http, url, headers, body)
        if isinstance(response, DeliveryError):
            return response
        failed = refused(response)
        if failed is not None:
            return failed
        messages = body_of(response).get("messages")
        first = messages[0] if isinstance(messages, list) and messages else None
        ref = first.get("id") if isinstance(first, dict) else None
        return Sent(ref if isinstance(ref, str) else "")

    async def lookup(self, effect_key: str, op: JsonObject) -> LookupResult[str]:
        return LookupUnknown("the WhatsApp Cloud API has no lookup by effect key")


def _card(text: str, challenge: str) -> JsonValue:
    """Reply buttons whose ids carry only the challenge id."""
    buttons: list[JsonValue] = [
        {"type": "reply", "reply": {"id": f"{verb}:{challenge}", "title": title}}
        for verb, title in (("approve", "Approve"), ("deny", "Deny"))
    ]
    return {"type": "button", "body": {"text": text}, "action": {"buttons": buttons}}


def _webhook(raw: RawRequest) -> _Webhook | None:
    try:
        return _Webhook.model_validate_json(raw.body)
    except ValidationError:
        return None


def _answer(button: str) -> _Button | None:
    """`approve:<challenge>` / `deny:<challenge>`, or TS's JSON form: all a button carries."""
    verb, _, challenge = button.partition(":")
    try:
        if verb in ("approve", "deny"):
            decision = "grant" if verb == "approve" else "deny"
            return _Button.model_validate({"challenge_id": challenge, "decision": decision})
        return _Button.model_validate_json(button)
    except ValidationError:
        return None


def _item(phone: str, message: _Message) -> Inbound:
    tenant = f"whatsapp:{phone}"
    who = Principal(issuer=tenant, tenant=tenant, subject=message.sender)
    pressed = message.interactive
    if pressed is not None and pressed.button_reply is not None:
        answer = _answer(pressed.button_reply.id)
        if answer is None:
            return Ignore(kind="ignore")
        return Decision(
            kind="decision",
            principal=who,
            address=message.sender,
            item_key=message.id,
            challenge_id=str(answer.challenge_id),
            decision=answer.decision,
        )
    if message.type != "text" or message.text is None or not message.text.body:
        return Ignore(kind="ignore")
    return Message(
        kind="message",
        principal=who,
        address=message.sender,
        item_key=message.id,
        content=message.text.body,
    )


def whatsapp(  # noqa: PLR0913 - the Cloud API's settings
    *,
    app_secret: Secret,
    access_token: Secret,
    verify_token: Secret,
    phone_number_id: str,
    agent: str,
    api: str = API,
    transport: httpx.AsyncBaseTransport | None = None,
) -> WhatsAppChannel:
    """A WhatsApp channel for host(channels=...). Secrets are resolved on the host."""
    return WhatsAppChannel(
        app_secret, access_token, verify_token, phone_number_id, agent, api, transport
    )
