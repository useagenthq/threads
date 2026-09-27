"""The host's HTTP API (spec/schema/host-api/openapi.json) as an ASGI app, on Starlette (the
`host` extra). Every /v1 route calls the library method its operation names with the
authenticated principal, scoped to that principal's tenant; `ready` and `stop` are its
lifespan."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from threads.host.a2a import route as a2a
from threads.host.app import Host
from threads.host.http import runs, teams, threads, ui_ag_ui, ui_ai_sdk, ui_frames
from threads.host.http.common import Handler


def app(host: Host) -> Starlette:
    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
        await host.ready()
        try:
            yield
        finally:
            await host.stop()

    routes = [
        # A2A (spec/schema/a2a/), before the /v1 table: the cards are discovery and the operations
        # settle the version and the caller from headers before any body is read.
        Route("/.well-known/agent-card.json", _sole_card(host), methods=["GET"]),
        Route("/a2a/{agent}/.well-known/agent-card.json", _agent_card(host), methods=["GET"]),
        Route("/a2a/{agent}", _rpc(host), methods=["POST"]),
        # DELETE too, so the push-notification config operations we do not serve answer by name
        # rather than as a verb this path does not take.
        Route("/a2a/{agent}/{path:path}", _http_json(host), methods=["GET", "POST", "DELETE"]),
        Route("/v1/runs", runs.start_run(host), methods=["POST"]),
        Route(
            "/v1/threads/{thread_id}/runs/{run_id}/events", runs.subscribe(host), methods=["GET"]
        ),
        Route("/v1/teams/{team}/events", teams.subscribe(host), methods=["GET"]),
        Route("/channels/{channel}/events", runs.webhook(host), methods=["POST"]),
        Route("/channels/{channel}/events", runs.challenge(host), methods=["GET"]),
        *threads.routes(host),
        # Web UIs (spec/schema/ui/README.md): the AI SDK UI message stream and AG-UI.
        Route("/v1/ui/ai-sdk/{agent}", ui_ai_sdk.chat(host), methods=["POST"]),
        Route("/v1/ui/ai-sdk/{agent}/{chat_id}/stream", ui_ai_sdk.reconnect(host), methods=["GET"]),
        Route("/v1/ui/ag-ui/{agent}", ui_ag_ui.run(host), methods=["POST"]),
        Route(
            "/v1/threads/{thread_id}/runs/{run_id}/ui/{protocol}",
            ui_frames.frames(host),
            methods=["GET"],
        ),
    ]
    return Starlette(routes=routes, lifespan=lifespan)


def _sole_card(host: Host) -> Handler:
    async def handle(request: Request) -> Response:
        return await a2a.sole_card(host, request)

    return handle


def _agent_card(host: Host) -> Handler:
    async def handle(request: Request) -> Response:
        return await a2a.agent_card(host, request)

    return handle


def _rpc(host: Host) -> Handler:
    async def handle(request: Request) -> Response:
        return await a2a.operation_route(host, request, "JSONRPC")

    return handle


def _http_json(host: Host) -> Handler:
    async def handle(request: Request) -> Response:
        return await a2a.operation_route(host, request, "HTTP+JSON")

    return handle
