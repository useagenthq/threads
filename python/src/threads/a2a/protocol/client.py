"""One A2A request. The adapter owns the bytes, so the caller sees exactly which of five outcomes it
got, and those five are the ones the effect machinery needs:

    Answered   the peer answered; the result is parsed a layer up
    Streamed   the peer answered SSE; the items are parsed StreamResponses
    Faulted    the peer answered an A2A error, which is an answer, not a doubt
    NotSent    the transport proved no byte was written
    Uncertain  anything else after dispatch: a timeout, or a connection that opened and broke

`NotSent` is deliberately narrow. The host transport already records this distinction for us:
`WebError.sent` is False only when the connect (including the TLS handshake) failed, and True for
everything after the first byte could have gone out. Treating a doubtful case as "not sent" would
let an effect repeat silently (invariant 3), so we take that flag and never widen it.

An unparsable answer is a **fault**, not uncertainty: the peer replied, so nothing is in doubt about
whether it received us."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal
from uuid import uuid4

from pydantic import JsonValue, ValidationError

from threads._generated.a2a_v1 import StreamResponse
from threads.a2a.protocol.errors import (
    A2aFault,
    error_by_code,
    error_info_in,
    fault,
    http_status,
)
from threads.a2a.protocol.jsonrpc import is_envelope, rpc_outcome
from threads.a2a.protocol.sse import sse_events_of
from threads.a2a.protocol.version import A2A_JSON, A2A_VERSION, EXTENSIONS_HEADER, VERSION_HEADER
from threads.a2a.protocol.wire import Method, Wire, outbound, parse_json, streams
from threads.result import Err
from threads.web.guard import Resolve, system_resolve, vet
from threads.web.http import Fence, Request, Response, StdlibTransport, Transport

_OK: Final = 200
_CLIENT_ERROR: Final = 400
_UNAUTHORIZED: Final = 401

MAX_BYTES: Final = 1 << 20
"""A response body over this many bytes is refused: a card or a task is small."""


@dataclass(frozen=True, slots=True)
class Answered:
    value: JsonValue


@dataclass(frozen=True, slots=True)
class Streamed:
    items: tuple[StreamResponse, ...]


@dataclass(frozen=True, slots=True)
class Faulted:
    fault: A2aFault


@dataclass(frozen=True, slots=True)
class NotSent:
    why: str


@dataclass(frozen=True, slots=True)
class Uncertain:
    reason: Literal["timeout", "transport_error"]
    why: str


type Answer = Answered | Streamed | Faulted | NotSent | Uncertain


@dataclass(frozen=True, slots=True)
class Sending:
    """How one request goes out. The credential is resolved at send time and never stored."""

    timeout_ms: int
    authorization: str | None = None
    extensions: Sequence[str] = ()
    body: str | None = None
    """The exact request body to send, instead of serializing `params`: a re-dispatch replays the
    bytes its first attempt stored, so a peer that deduplicates on `messageId` sees one message
    (30-a2a decision H30-1). Ignored by an operation whose request has no body."""
    transport: Transport | None = None
    resolve: Resolve | None = None
    fence: Fence | None = None


async def _always_fenced() -> bool:
    return True


def _headers(accept: str, has_body: bool, sending: Sending) -> dict[str, str]:
    headers = {"Accept": accept, VERSION_HEADER: A2A_VERSION}
    if has_body:
        headers["Content-Type"] = A2A_JSON
    if sending.extensions:
        headers[EXTENSIONS_HEADER] = ",".join(sending.extensions)
    if sending.authorization is not None:
        headers["Authorization"] = sending.authorization
    return headers


async def call(
    wire: Wire, method: Method, params: dict[str, JsonValue], sending: Sending
) -> Answer:
    # One id per request, checked on the way back: an answer proves it answers us before it is read.
    built = outbound(wire, method, params, str(uuid4()))
    if not built.url.lower().startswith("https:"):
        return NotSent(f"a remote is called over https, not {built.url.split(':', 1)[0]}")
    target = await vet(built.url, sending.resolve or system_resolve)
    if isinstance(target, Err):
        return NotSent(target.error)
    transport = sending.transport or StdlibTransport(sending.timeout_ms / 1000)
    request = Request(
        built.verb,
        _headers(built.accept, built.body is not None, sending),
        None if built.body is None else (sending.body or built.body).encode(),
    )
    sent = await transport.send(target.value, request, sending.fence or _always_fenced, MAX_BYTES)
    if isinstance(sent, Err):
        return _failed(sent.error.code, sent.error.message, sent.error.sent)
    return _answered(method, sent.value, built.rpc_id)


def _answered(method: Method, response: Response, rpc_id: str | None) -> Answer:
    """A response the transport handed back, as one of the answers a caller sees."""
    if response.truncated:
        return Faulted(
            fault("InvalidAgentResponseError", f"the peer answered more than {MAX_BYTES} bytes")
        )
    # Strict: bytes that are not UTF-8 are a fault, never a U+FFFD we read on as if the peer had
    # sent it. The peer did answer, so a body we cannot decode is an answer we cannot read and
    # nothing is in doubt about whether it received us.
    try:
        text = response.body.decode()
    except UnicodeDecodeError:
        return Faulted(fault("InvalidAgentResponseError", "the response body is not valid UTF-8"))
    if streams(method) and response.status == _OK and _is_event_stream(response.headers):
        return _stream(text, rpc_id)
    return _single(response.status, text, rpc_id)


def _failed(code: str, message: str, was_sent: bool) -> Answer:
    if not was_sent:
        # The connect or the TLS handshake failed, or the fence refused before a byte was written.
        return NotSent(f"{code}: {message}")
    return Uncertain("timeout" if code == "timeout" else "transport_error", f"{code}: {message}")


def _is_event_stream(headers: Mapping[str, str]) -> bool:
    return "text/event-stream" in headers.get("content-type", "")


def _single(status: int, text: str, sent: str | None) -> Answer:
    """A non-streaming answer: the operation's own message, or the A2A error the peer named."""
    parsed = parse_json(text)
    if isinstance(parsed, Err):
        return Faulted(parsed.error)
    body = parsed.value
    if is_envelope(body):
        outcome = rpc_outcome(body, sent)
        return Faulted(outcome.error) if isinstance(outcome, Err) else Answered(outcome.value)
    if status >= _CLIENT_ERROR:
        return Faulted(_http_fault(status, body))
    return Answered(body)


def _http_fault(status: int, body: JsonValue) -> A2aFault:
    """An HTTP+JSON error body. The binding answers a `google.rpc.Status`, whose `details` MUST
    carry a `google.rpc.ErrorInfo`: the reason is what names the error there, so a body without one
    we recognise is a response we cannot read rather than an error we can act on. The JSON-RPC code
    is read too, where an implementation puts one, and the two must agree with each other and with
    the status the pinned table gives that error."""
    found = error_info_in(body.get("details") if isinstance(body, dict) else None)
    if found is None:
        return fault(
            "InvalidAgentResponseError",
            f"the peer answered HTTP {status} with no google.rpc.ErrorInfo",
        )
    named = found.named
    if named is None:
        return fault(
            "InvalidAgentResponseError",
            f"the peer answered HTTP {status} with ErrorInfo reason {found.info.reason}, "
            f"which is not an A2A error",
        )
    # A 401 is the one status that overrides the table: it is a challenge a client has to see, so
    # whatever error the peer names, the status it arrives with is 401 rather than that error's own.
    if status != _UNAUTHORIZED and http_status(named) != status:
        return fault(
            "InvalidAgentResponseError",
            f"the peer answered HTTP {status} with ErrorInfo reason {found.info.reason}, "
            f"which is HTTP {http_status(named)}",
        )
    code = _number_at(body, "code")
    by_code = None if code is None else error_by_code(code)
    if by_code is not None and by_code != named:
        return fault(
            "InvalidAgentResponseError",
            f"the peer answered HTTP {status} with code {code} and ErrorInfo reason "
            f"{found.info.reason}, which name different errors",
        )
    return fault(named, _string_at(body, "message") or f"the peer answered HTTP {status}")


def _at(body: JsonValue, key: str) -> JsonValue:
    if isinstance(body, dict):
        own = body.get(key)
        if own is not None:
            return own
        nested = body.get("error")
        if isinstance(nested, dict):
            return nested.get(key)
    return None


def _number_at(body: JsonValue, key: str) -> int | None:
    found = _at(body, key)
    return found if isinstance(found, int) and not isinstance(found, bool) else None


def _string_at(body: JsonValue, key: str) -> str | None:
    found = _at(body, key)
    return found if isinstance(found, str) else None


def _stream(text: str, sent: str | None) -> Answer:
    """A peer's SSE body as parsed `StreamResponse` items.

    A frame we cannot read is a fault, not a short stream. This transport hands us the whole body
    at once, so there is no prefix already delivered to a caller and nothing to salvage: a frame
    that is not JSON, an envelope answering someone else, or a payload that is not a
    `StreamResponse` all mean the peer sent us something we cannot read, and a caller that saw a
    short tuple could not tell that from a stream that simply ended."""
    items: list[StreamResponse] = []
    for event in sse_events_of(text):
        parsed = parse_json(event.data)
        if isinstance(parsed, Err):
            return Faulted(parsed.error)
        body = parsed.value
        if is_envelope(body):
            outcome = rpc_outcome(body, sent)
            if isinstance(outcome, Err):
                return Faulted(outcome.error)
            body = outcome.value
        try:
            items.append(StreamResponse.model_validate(body))
        except ValidationError:
            return Faulted(
                fault("InvalidAgentResponseError", "a stream frame is not a StreamResponse")
            )
    return Streamed(tuple(items))


@dataclass(frozen=True, slots=True)
class Fetched:
    bytes_: bytes | None
    why: str | None
    """Set when the card could not be read; `bytes_` is then None."""


async def fetch_bytes(url: str, max_bytes: int, sending: Sending) -> Fetched:
    """A GET of `url`, up to `max_bytes`, **with no credential**: this is how a partner's agent card
    is read, and discovery is unauthenticated by the spec. It is a read, so it is not an effect and
    it retries freely."""
    if not url.lower().startswith("https:"):
        return Fetched(None, f"a card is fetched over https, not {url.split(':', 1)[0]}")
    target = await vet(url, sending.resolve or system_resolve)
    if isinstance(target, Err):
        return Fetched(None, target.error)
    transport = sending.transport or StdlibTransport(sending.timeout_ms / 1000)
    request = Request("GET", {"Accept": f"{A2A_JSON}, application/json"}, None)
    sent = await transport.send(target.value, request, sending.fence or _always_fenced, max_bytes)
    if isinstance(sent, Err):
        return Fetched(None, sent.error.message)
    if sent.value.status != _OK:
        return Fetched(None, f"HTTP {sent.value.status}")
    if sent.value.truncated:
        return Fetched(None, f"more than {max_bytes} bytes")
    return Fetched(sent.value.body, None)
