"""POST /v1/runs, the run's SSE stream, and the channel webhook."""

from collections.abc import AsyncIterator

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from threads._generated.host_api_v1 import StartRunRequest
from threads.host.app import Host
from threads.host.channel import RawRequest
from threads.host.http.common import Handler, authenticated, body, error, failed
from threads.host.stream import Message
from threads.log import EventId, Principal, ThreadId
from threads.log.jcs import canonicalize
from threads.reduce.handlers import to_json
from threads.result import Err, Ok


def start_run(host: Host) -> Handler:
    """202 with the receipt once the user_input is durable (Host.start_run)."""

    async def handle(request: Request, principal: Principal) -> Response:
        key = request.headers.get("idempotency-key")
        if key is None:
            return error("invalid_request", "Idempotency-Key is required")
        parsed = await body(request, StartRunRequest)
        if isinstance(parsed, JSONResponse):
            return parsed
        started = await host.start_run(parsed, principal=principal, idempotency_key=key)
        if isinstance(started, Err):
            return failed(started.error)
        return JSONResponse(to_json(started.value), status_code=202)

    return authenticated(host, handle)


def subscribe(host: Host) -> Handler:
    """The run's events, each with its seq as the SSE id, then the result (Host.subscribe)."""

    async def handle(request: Request, principal: Principal) -> Response:
        after = request.headers.get("last-event-id") or request.query_params.get("after_seq")
        if after is not None and not after.isdigit():
            return error("not_found", "after_seq is not a seq")
        thread = ThreadId(request.path_params["thread_id"])
        run = EventId(request.path_params["run_id"])
        seq = int(after or 0)
        followed = await host.subscribe(thread, run, principal=principal, after_seq=seq)
        if isinstance(followed, Err):
            return failed(followed.error)
        return StreamingResponse(_sse(followed.value), media_type="text/event-stream")

    return authenticated(host, handle)


async def _sse(messages: AsyncIterator[Message]) -> AsyncIterator[str]:
    async for message in messages:
        data = canonicalize(message.data)
        if not isinstance(data, Ok):
            raise AssertionError("an SSE message is JSON")
        head = "" if message.id is None else f"id: {message.id}\n"
        yield f"{head}data: {data.value}\n\n"


def webhook(host: Host) -> Handler:
    """Channel intake: raw bytes and headers to the adapter, the batch durable, then its ack.
    Not bearer-authenticated: the adapter verifies the provider's signature."""

    async def handle(request: Request) -> Response:
        headers = {k.lower(): v for k, v in request.headers.items()}
        raw = RawRequest(headers, await request.body())
        answered = await host.receive(request.path_params["channel"], raw)
        if isinstance(answered, Err):
            return failed(answered.error)
        reply = answered.value
        return Response(reply.body, reply.status, headers=dict(reply.headers))

    return handle


def challenge(host: Host) -> Handler:
    """A provider's GET subscription check on the webhook URL; not bearer-authenticated."""

    async def handle(request: Request) -> Response:
        query = dict(request.query_params)
        answered = host.challenge(request.path_params["channel"], query)
        if isinstance(answered, Err):
            return failed(answered.error)
        reply = answered.value
        return Response(reply.body, reply.status, headers=dict(reply.headers))

    return handle
