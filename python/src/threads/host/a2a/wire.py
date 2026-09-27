"""How an answer and a refusal look on each binding. The status and the code always come from the
protocol core's pinned table, never from a number written here."""

import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Final

from pydantic import JsonValue
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from threads.a2a.protocol import (
    A2A_JSON,
    VERSION_HEADER,
    A2aFault,
    Binding,
    RpcId,
    SseFrame,
    error_info,
    fault,
    http_status,
    json_rpc_code,
    rpc_fault,
    rpc_result,
    sse_body,
)

_OK: Final = 200
_UNAUTHORIZED: Final = 401


@dataclass(frozen=True, slots=True)
class Envelope:
    binding: Binding
    id: RpcId = None
    """The JSON-RPC request's id, echoed back; None on the HTTP+JSON binding."""


def _text(body: str, status: int, content_type: str, more: Mapping[str, str] = {}) -> Response:
    return Response(body, status_code=status, media_type=content_type, headers=dict(more))


def answer(envelope: Envelope, value: JsonValue) -> Response:
    if envelope.binding == "JSONRPC":
        return _text(rpc_result(envelope.id, value), _OK, "application/json")
    return _text(json.dumps(value), _OK, A2A_JSON)


def _http_body(f: A2aFault) -> str:
    """The HTTP+JSON error body: a `google.rpc.Status` whose `details` carry the
    `google.rpc.ErrorInfo` the binding requires. The status names the error and the reason names it
    again in the form the other binding uses, so a partner reading either one agrees with us; `code`
    stays the JSON-RPC code, which is the number both of our bindings and the pinned table speak."""
    return json.dumps(
        {
            "code": json_rpc_code(f.name),
            "message": f"{f.name}: {f.message}",
            "details": [error_info(f.name)],
        }
    )


def refuse(envelope: Envelope, f: A2aFault) -> Response:
    """A refusal. JSON-RPC answers HTTP 200 with the error in its envelope; HTTP+JSON answers the
    status from the pinned table with a body carrying the same JSON-RPC code."""
    if envelope.binding == "JSONRPC":
        return _text(rpc_fault(envelope.id, f), _OK, "application/json")
    return _text(_http_body(f), http_status(f.name), A2A_JSON)


def unauthenticated(envelope: Envelope) -> Response:
    """No principal: HTTP 401 with a challenge, and the binding's own error envelope. A 401 is the
    one refusal that leaves the binding's usual status behind, because a client has to see it."""
    f = fault("InvalidRequestError", "this request is not authenticated")
    body = rpc_fault(envelope.id, f) if envelope.binding == "JSONRPC" else _http_body(f)
    content = "application/json" if envelope.binding == "JSONRPC" else A2A_JSON
    return _text(body, _UNAUTHORIZED, content, {"WWW-Authenticate": "Bearer"})


def streamed(frames: AsyncIterator[SseFrame]) -> Response:
    """A streamed answer: each SSE item is the binding's own form of one StreamResponse."""
    return StreamingResponse(
        sse_body(frames),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


def item_of(envelope: Envelope, item: JsonValue) -> str:
    """One stream item as its `data:` line: wrapped in the JSON-RPC envelope, or bare."""
    return rpc_result(envelope.id, item) if envelope.binding == "JSONRPC" else json.dumps(item)


def version_of(request: Request) -> tuple[str | None, str | None]:
    """The version a request declares: the header, else the query parameter the spec also allows."""
    return (request.headers.get(VERSION_HEADER), request.query_params.get(VERSION_HEADER))


def card_response(body: bytes) -> Response:
    """The card, which is discovery: no version, no principal, and never cached."""
    return Response(
        body, status_code=_OK, media_type=A2A_JSON, headers={"cache-control": "no-cache"}
    )


def not_found(binding: Binding) -> Response:
    """A path that is none of ours, and an agent this host does not expose: the same answer, so a
    caller cannot probe which agents we run."""
    return refuse(
        Envelope(binding),
        fault("MethodNotFoundError", "no such A2A operation on this host"),
    )


def json_error(f: A2aFault) -> Response:
    """A refusal on a route with no binding yet, for the rare case of a malformed path."""
    return Response(
        _http_body(f),
        status_code=http_status(f.name),
        media_type="application/json",
    )
