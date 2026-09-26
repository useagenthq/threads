"""GET /v1/teams/{team}/events: a lead team's feed as server-sent events (lane 29B). Each item
is one message, `id: <epoch>:<offset>` and the item as RFC 8785 JSON. A `: keepalive` comment
every 15 s keeps proxies open. On epoch_restarted the route sends that item and closes, so the
client reconnects with the new cursor."""

import asyncio
from collections.abc import AsyncIterator

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from threads.agents.team_handle_types import EpochRestarted, TeamItem
from threads.host.app import Host
from threads.host.http.common import Handler, authenticated, error, failed
from threads.host.team_stream import KEEPALIVE_S, item_json, team_cursor
from threads.log import Principal
from threads.log.jcs import canonicalize
from threads.result import Err, Ok


def subscribe(host: Host, keepalive_s: float = KEEPALIVE_S) -> Handler:
    """The team's items, each with its `<epoch>:<offset>` as the SSE id."""

    async def handle(request: Request, principal: Principal) -> Response:
        after = team_cursor(request.headers.get("last-event-id"), request.query_params.get("after"))
        if isinstance(after, Err):
            return error(after.error.code, after.error.message)
        followed = await host.subscribe_team(
            request.path_params["team"], principal=principal, after=after.value
        )
        if isinstance(followed, Err):
            return failed(followed.error)
        return StreamingResponse(
            sse_frames(followed.value, keepalive_s), media_type="text/event-stream"
        )

    return authenticated(host, handle)


async def sse_frames(items: AsyncIterator[TeamItem], keepalive_s: float) -> AsyncIterator[str]:
    """One read at a time: a keepalive is due while the same read is still pending, so it never
    starts a second one and never drops an item."""
    pending: asyncio.Task[TeamItem] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(items))
            done, _ = await asyncio.wait([pending], timeout=keepalive_s)
            if not done:
                yield ": keepalive\n\n"
                continue
            read, pending = pending, None
            try:
                item = read.result()
            except StopAsyncIteration:
                return
            yield _frame(item)
            # The client reconnects with the new cursor (lane 25 Part B's rule).
            if isinstance(item, EpochRestarted):
                return
    finally:
        if pending is not None:
            pending.cancel()


def _frame(item: TeamItem) -> str:
    body = canonicalize(item_json(item))
    if not isinstance(body, Ok):
        raise AssertionError("a team item is JSON")
    return f"id: {item.cursor.epoch}:{item.cursor.offset}\ndata: {body.value}\n\n"
