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

from pydantic import JsonValue, ValidationError

from threads._generated.a2a_v1 import StreamResponse
from threads.a2a.protocol.errors import A2aFault, error_by_code, fault
from threads.a2a.protocol.jsonrpc import is_envelope, rpc_outcome
from threads.a2a.protocol.sse import sse_events_of
from threads.a2a.protocol.version import A2A_JSON, A2A_VERSION, EXTENSIONS_HEADER, VERSION_HEADER
from threads.a2a.protocol.wire import Method, Wire, outbound, parse_json, streams
from threads.result import Err
from threads.web.guard import Resolve, system_resolve, vet
from threads.web.http import Fence, Request, StdlibTransport, Transport

_OK: Final = 200
_CLIENT_ERROR: Final = 400

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
    built = outbound(wire, method, params)
    if not built.url.lower().startswith("https:"):
        return NotSent(f"a remote is called over https, not {built.url.split(':', 1)[0]}")
    target = await vet(built.url, sending.resolve or system_resolve)
    if isinstance(target, Err):
        return NotSent(target.error)
    transport = sending.transport or StdlibTransport(sending.timeout_ms / 1000)
    request = Request(
        built.verb,
        _headers(built.accept, built.body is not None, sending),
        None if built.body is None else built.body.encode(),
    )
    sent = await transport.send(target.value, request, sending.fence or _always_fenced, MAX_BYTES)
    if isinstance(sent, Err):
        return _failed(sent.error.code, sent.error.message, sent.error.sent)
    response = sent.value
    if response.truncated:
        return Faulted(
            fault("InvalidAgentResponseError", f"the peer answered more than {MAX_BYTES} bytes")
        )
    text = response.body.decode(errors="replace")
    if streams(method) and response.status == _OK and _is_event_stream(response.headers):
        return _stream(text)
    return _single(response.status, text)


def _failed(code: str, message: str, was_sent: bool) -> Answer:
    if not was_sent:
        # The connect or the TLS handshake failed, or the fence refused before a byte was written.
        return NotSent(f"{code}: {message}")
    return Uncertain("timeout" if code == "timeout" else "transport_error", f"{code}: {message}")


def _is_event_stream(headers: Mapping[str, str]) -> bool:
    return "text/event-stream" in headers.get("content-type", "")


def _single(status: int, text: str) -> Answer:
    """A non-streaming answer: the operation's own message, or the A2A error the peer named."""
    parsed = parse_json(text)
    if isinstance(parsed, Err):
        return Faulted(parsed.error)
    body = parsed.value
    if is_envelope(body):
        outcome = rpc_outcome(body)
        return Faulted(outcome.error) if isinstance(outcome, Err) else Answered(outcome.value)
    if status >= _CLIENT_ERROR:
        return Faulted(_http_fault(status, body))
    return Answered(body)


def _http_fault(status: int, body: JsonValue) -> A2aFault:
    """An HTTP+JSON error body. The binding carries the A2A error in the status, and
    implementations put the code either at the top level or under `error`, so both are read; a body
    that names no code we know is the status, said plainly."""
    code = _number_at(body, "code")
    message = _string_at(body, "message")
    named = None if code is None else error_by_code(code)
    if named is None:
        return fault(
            "InvalidAgentResponseError",
            f"the peer answered HTTP {status} with no A2A error code",
        )
    return fault(named, message or f"the peer answered HTTP {status}")


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


def _stream(text: str) -> Answer:
    """A peer's SSE body as parsed `StreamResponse` items; an unparsable item ends the stream rather
    than being guessed at."""
    items: list[StreamResponse] = []
    for event in sse_events_of(text):
        parsed = parse_json(event.data)
        if isinstance(parsed, Err):
            break
        body = parsed.value
        if is_envelope(body):
            outcome = rpc_outcome(body)
            if isinstance(outcome, Err):
                break
            body = outcome.value
        try:
            items.append(StreamResponse.model_validate(body))
        except ValidationError:
            break
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
