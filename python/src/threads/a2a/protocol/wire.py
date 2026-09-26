"""Where each operation lives in each binding, and how a request's fields ride there. One table
for both directions: the client builds a request from it, and the exposed side matches a path with
it."""

import json
import re
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import quote, unquote, urlencode

from pydantic import JsonValue, TypeAdapter, ValidationError

from threads.a2a.protocol.errors import A2aFault, fault
from threads.a2a.protocol.version import A2A_VERSION
from threads.result import Err, Ok

type Binding = Literal["JSONRPC", "HTTP+JSON"]
BINDINGS: Final[tuple[Binding, ...]] = ("JSONRPC", "HTTP+JSON")

type Method = Literal[
    "SendMessage",
    "SendStreamingMessage",
    "GetTask",
    "ListTasks",
    "CancelTask",
    "SubscribeToTask",
]
METHODS: Final[tuple[Method, ...]] = (
    "SendMessage",
    "SendStreamingMessage",
    "GetTask",
    "ListTasks",
    "CancelTask",
    "SubscribeToTask",
)


def binding_of(protocol_binding: str) -> Binding | None:
    return next((b for b in BINDINGS if b == protocol_binding), None)


def method_of(name: str) -> Method | None:
    """A method name we recognise, or None: an unknown one is MethodNotFoundError."""
    return next((m for m in METHODS if m == name), None)


@dataclass(frozen=True, slots=True)
class Http:
    verb: Literal["GET", "POST"]
    path: str
    """The path under the interface's base URL, with `{id}` where the task id goes."""
    where: Literal["body", "query"]
    """Whether the request message rides in the body (POST) or the query string (GET)."""
    streams: bool


HTTP: Final[dict[Method, Http]] = {
    "SendMessage": Http("POST", "/message:send", "body", streams=False),
    "SendStreamingMessage": Http("POST", "/message:stream", "body", streams=True),
    "GetTask": Http("GET", "/tasks/{id}", "query", streams=False),
    "ListTasks": Http("GET", "/tasks", "query", streams=False),
    "CancelTask": Http("POST", "/tasks/{id}:cancel", "body", streams=False),
    # The proto's HTTP annotation says GET; the specification's prose table says POST. The proto is
    # normative, so we send GET and accept both inbound (spec/schema/a2a/README.md).
    "SubscribeToTask": Http("GET", "/tasks/{id}:subscribe", "query", streams=True),
}


def streams(method: Method) -> bool:
    """The two operations whose answer is an SSE stream."""
    return HTTP[method].streams


@dataclass(frozen=True, slots=True)
class Wire:
    """The pinned interface a card chose: where to send, and in which binding."""

    url: str
    binding: Binding


@dataclass(frozen=True, slots=True)
class Outbound:
    url: str
    verb: Literal["GET", "POST"]
    body: str | None
    accept: str


_STREAM_ACCEPT: Final = "text/event-stream"
_JSON_ACCEPT: Final = "application/a2a+json, application/json"


def outbound(wire: Wire, method: Method, params: dict[str, JsonValue]) -> Outbound:
    """The HTTP request one call becomes. `id` is taken out of the message for the path; every
    other field of a GET operation becomes a query parameter."""
    accept = _STREAM_ACCEPT if streams(method) else _JSON_ACCEPT
    if wire.binding == "JSONRPC":
        envelope = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        return Outbound(wire.url, "POST", json.dumps(envelope), accept)
    shape = HTTP[method]
    base = wire.url.rstrip("/")
    raw_id = params.get("id")
    path = shape.path.replace("{id}", quote(raw_id if isinstance(raw_id, str) else "", safe=""))
    if shape.where == "body":
        return Outbound(f"{base}{path}", shape.verb, json.dumps(params), accept)
    query = [
        (key, value if isinstance(value, str) else json.dumps(value))
        for key, value in params.items()
        if key != "id" and value is not None
    ]
    # Also as a query parameter, which the spec allows and some clients prefer.
    query.append(("A2A-Version", A2A_VERSION))
    return Outbound(f"{base}{path}?{urlencode(query)}", shape.verb, None, accept)


_JSON: Final = TypeAdapter[JsonValue](JsonValue)


def parse_json(text: str) -> Ok[JsonValue] | Err[A2aFault]:
    """A body as JSON, or the JSONParseError it earns. Tagged rather than a union with the payload:
    a legitimate payload can have a `name` field of its own."""
    try:
        return Ok(_JSON.validate_json(text))
    except ValidationError:
        return Err(fault("JSONParseError", "the payload is not valid JSON"))


@dataclass(frozen=True, slots=True)
class Inbound:
    method: Method
    id: str | None


_TASK_PATH: Final = re.compile(r"^/tasks/([^/]+?)(:cancel|:subscribe)?$")
_SUFFIX: Final[dict[str | None, Method]] = {
    ":cancel": "CancelTask",
    ":subscribe": "SubscribeToTask",
    None: "GetTask",
}


def inbound_path(rest: str) -> Inbound | None:
    """The operation an inbound HTTP+JSON path names, with the task id it carries. None means the
    path is not one of ours; a known path with the wrong verb is a MethodNotFoundError at the
    route."""
    if rest == "/message:send":
        return Inbound("SendMessage", None)
    if rest == "/message:stream":
        return Inbound("SendStreamingMessage", None)
    if rest == "/tasks":
        return Inbound("ListTasks", None)
    found = _TASK_PATH.match(rest)
    if found is None:
        return None
    task_id = unquote(found.group(1))
    return Inbound(_SUFFIX[found.group(2)], task_id)
