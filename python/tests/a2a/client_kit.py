"""The peers the client tests drive. A transport here records what it was asked to send and answers
like a real peer would - an envelope has to echo the id it was asked with, because a transport that
answered a fixed id could not tell a client that checks correlation from one that does not.

Mirrors typescript/packages/a2a/test/kit.ts."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import JsonValue, TypeAdapter

from threadsai.a2a.protocol import (
    A2aErrorName,
    Sending,
    Wire,
    error_info,
)
from threadsai.result import Err, Ok
from threadsai.web.guard import Resolve, Target
from threadsai.web.http import Fence, Request, Response, Transport, WebError, WebErrorCode

RPC = Wire("https://partner.example/a2a/refunds", "JSONRPC")
REST = Wire("https://partner.example/a2a/refunds", "HTTP+JSON")

TASK: JsonValue = {"id": "task-1", "contextId": "ctx-1", "status": {"state": "TASK_STATE_WORKING"}}


async def public_address(_host: str, _port: int) -> Sequence[str]:
    return ("93.184.216.34",)


async def private_address(_host: str, _port: int) -> Sequence[str]:
    return ("127.0.0.1",)


@dataclass(slots=True)
class Answering:
    """A transport that answers one response and records what it was asked to send."""

    response: Response
    sent: list[tuple[Target, Request]] = field(default_factory=list[tuple[Target, Request]])

    async def send(
        self, target: Target, request: Request, fence: Fence, max_bytes: int
    ) -> Ok[Response] | Err[WebError]:
        assert await fence()
        assert max_bytes > 0
        self.sent.append((target, request))
        return Ok(self.response)


@dataclass(slots=True)
class Failing:
    """A transport that fails, saying whether any request byte may have gone out."""

    code: WebErrorCode
    was_sent: bool
    sent: list[tuple[Target, Request]] = field(default_factory=list[tuple[Target, Request]])

    async def send(
        self, target: Target, request: Request, fence: Fence, max_bytes: int
    ) -> Ok[Response] | Err[WebError]:
        assert max_bytes > 0
        await fence()
        self.sent.append((target, request))
        return Err(WebError(self.code, f"{self.code} at {target.host}", sent=self.was_sent))


def json_response(body: object, status: int = 200) -> Response:
    return Response(status, {"content-type": "application/json"}, json.dumps(body).encode())


_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


def sent_id(request: Request) -> JsonValue:
    """The JSON-RPC id a request carried. A peer has to echo this, and ours has to check it did."""
    body = _JSON.validate_json(request.body or b"{}")
    return body.get("id") if isinstance(body, dict) else None


@dataclass(slots=True)
class Echoing:
    """A peer that answers in the envelope, echoing the id it was asked with, as a peer must.

    `member` is the envelope's `result` or `error`, by key. A transport that answered a fixed id
    could not tell a client that checks correlation from one that does not."""

    member: Mapping[str, JsonValue]
    status: int = 200
    frames: tuple[str, ...] = ()
    """When set, an SSE body of these `data:` lines instead, with `{id}` replaced by our id."""
    sent: list[tuple[Target, Request]] = field(default_factory=list[tuple[Target, Request]])

    async def send(
        self, target: Target, request: Request, fence: Fence, max_bytes: int
    ) -> Ok[Response] | Err[WebError]:
        assert await fence()
        assert max_bytes > 0
        self.sent.append((target, request))
        rpc_id = json.dumps(sent_id(request))
        if self.frames:
            body = "".join(f"data: {f.replace('{id}', rpc_id)}\n\n" for f in self.frames)
            return Ok(Response(200, {"content-type": "text/event-stream"}, body.encode()))
        envelope = {"jsonrpc": "2.0", "id": sent_id(request), **self.member}
        return Ok(json_response(envelope, self.status))


def http_error(name: A2aErrorName, status: int) -> Response:
    """An HTTP+JSON error body as the binding requires it: a Status whose details name the error."""
    return json_response(
        {"code": -1, "message": "no", "details": [error_info(name)]}, status=status
    )


def stream_response(body: str) -> Response:
    return Response(200, {"content-type": "text/event-stream"}, body.encode())


def sending(
    transport: Transport,
    *,
    resolve: Resolve = public_address,
    authorization: str | None = None,
    extensions: tuple[str, ...] = (),
) -> Sending:
    return Sending(
        timeout_ms=5_000,
        authorization=authorization,
        extensions=extensions,
        transport=transport,
        resolve=resolve,
    )
