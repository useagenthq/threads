"""`whatsapp()` (extra `whatsapp`, ): the WhatsApp Cloud API.

A webhook is verified by `X-Hub-Signature-256` over the raw bytes with the app secret, and the
URL's GET subscription check by the verify token, compared in constant time. The
WhatsApp Business Account is the tenant. One webhook may batch several messages, each keyed by
its own `wamid`, so none is dropped as a duplicate of a neighbor. Outbound sends carry the
effect key as `biz_opaque_callback_data`. The Cloud API has no lookup by that key, so an
uncertain send parks. Meta publishes no official Python SDK; the REST
API is called through the fenced httpx client.
"""

import hashlib
import hmac
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

import httpx
from pydantic import Field, JsonValue, ValidationError

from threads.adapters.channels.common import (
    Loose,
    body_of,
    client,
    final_text,
    hub_signature,
    refused,
    send,
    unverified,
)
from threads.host.channel import (
    ChannelCapabilities,
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


class _Message(Loose):
    id: str
    sender: str = Field(alias="from")
    type: str
    text: _Text | None = None


class _Value(Loose):
    messages: tuple[_Message, ...] = ()


class _Change(Loose):
    value: _Value


class _Entry(Loose):
    id: str
    changes: tuple[_Change, ...] = ()


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
        default_factory=lambda: ChannelCapabilities("none", False, False, True, True)
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
        if hook is None or not hook.entry:
            return unverified("not a WhatsApp webhook")
        account = hook.entry[0].id
        # Meta sends no delivery id: the signed bytes identify the delivery.
        return Ok(VerifiedDelivery(account, account, hashlib.sha256(raw.body).hexdigest()))

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
                items.extend(_item(entry.id, m) for m in change.value.messages)
        return Ok(items)

    def ack(self, raw: RawRequest) -> RawResponse:
        return RawResponse(200, {}, b"")

    def render(self, event: Event) -> Sequence[JsonObject]:
        text = final_text(event)
        return () if text is None else ({"text": text},)

    async def perform(
        self, op: JsonObject, effect_key: str, credentials: Mapping[str, str]
    ) -> DeliveryOutcome:
        body: JsonValue = {
            "messaging_product": "whatsapp",
            "to": op["address"],
            "type": "text",
            "text": {"body": op["text"]},
            "biz_opaque_callback_data": effect_key,
        }
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


def _webhook(raw: RawRequest) -> _Webhook | None:
    try:
        return _Webhook.model_validate_json(raw.body)
    except ValidationError:
        return None


def _item(account: str, message: _Message) -> Inbound:
    if message.type != "text" or message.text is None or not message.text.body:
        return Ignore(kind="ignore")
    return Message(
        kind="message",
        principal=Principal(issuer=f"whatsapp:{account}", tenant=account, subject=message.sender),
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
