"""What the channel adapters share: HMAC signature checks over the raw bytes, JSON parsing at
the webhook boundary, and one fenced send whose failures are classified by what can have
reached the provider.

`definite_not_sent` only when nothing can have arrived: the connection failed before the
request was written, or the provider answered that it rejected the request. Anything after the
request may have left (a timeout, a dropped connection, a 5xx) is `outcome_unknown`.
"""

import hashlib
import hmac
from collections.abc import Mapping
from http import HTTPStatus
from typing import Final

import httpx
from pydantic import JsonValue, TypeAdapter, ValidationError

from threads.adapters.memories.http import fenced_client
from threads.host.channel import DeliveryError, DeliveryOutcome
from threads.log import Event as LogEvent
from threads.log import ModelResponseEvent, ParseError, TextPart
from threads.result import Err, Ok

_OBJECT: Final[TypeAdapter[dict[str, JsonValue]]] = TypeAdapter(dict[str, JsonValue])
_BEFORE_SEND = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def hub_signature(secret: str, body: bytes, header: str | None) -> bool:
    """`X-Hub-Signature-256: sha256=<hex>` over the raw body (GitHub, Meta)."""
    if header is None or not header.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={digest}", header)


def json_object(body: bytes) -> Ok[dict[str, JsonValue]] | Err[ParseError]:
    try:
        return Ok(_OBJECT.validate_json(body))
    except ValidationError:
        return Err(ParseError("invalid", "the webhook body is not a JSON object"))


def unverified(why: str) -> Err[ParseError]:
    return Err(ParseError("unverified", why))


def final_text(event: LogEvent) -> str | None:
    """The text a final response shows, or None for any other event."""
    if not isinstance(event, ModelResponseEvent):
        return None
    text = "".join(p.text for p in event.data.content if isinstance(p, TextPart)).strip()
    return text or None


def client(transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    """The fenced client: a send outside the run's bound fence is refused before any byte."""
    return fenced_client(transport)


async def send(
    http: httpx.AsyncClient, url: str, headers: Mapping[str, str], body: JsonValue
) -> httpx.Response | DeliveryError:
    """One POST, never retried here: the effect path decides what an error means."""
    try:
        return await http.post(url, headers=dict(headers), json=body)
    except _BEFORE_SEND:
        return DeliveryError("transient", "definite_not_sent")
    except httpx.HTTPError:
        return DeliveryError("transient", "outcome_unknown")


def refused(response: httpx.Response) -> DeliveryOutcome | None:
    """A provider's error status as a delivery error; None for a success."""
    status = response.status_code
    if status < HTTPStatus.BAD_REQUEST:
        return None
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        return DeliveryError("rate_limited", "definite_not_sent")
    if status < HTTPStatus.INTERNAL_SERVER_ERROR:
        return DeliveryError("permanent", "definite_not_sent")
    # A 5xx may come after the provider acted on the request.
    return DeliveryError("transient", "outcome_unknown")


def body_of(response: httpx.Response) -> dict[str, JsonValue]:
    """A success response's JSON object; an unreadable one is an empty object."""
    parsed = json_object(response.content)
    return parsed.value if isinstance(parsed, Ok) else {}
