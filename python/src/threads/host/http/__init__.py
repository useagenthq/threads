"""The host's HTTP API (spec/schema/host-api/openapi.json) as an ASGI app, on Starlette (the
`host` extra). Every /v1 route calls the library method its operation names with the
authenticated principal, scoped to that principal's tenant; `ready` and `stop` are its
lifespan."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.routing import Route

from threads.host.app import Host
from threads.host.http import runs, threads, ui_ag_ui, ui_ai_sdk, ui_frames


def app(host: Host) -> Starlette:
    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
        await host.ready()
        try:
            yield
        finally:
            await host.stop()

    routes = [
        Route("/v1/runs", runs.start_run(host), methods=["POST"]),
        Route(
            "/v1/threads/{thread_id}/runs/{run_id}/events", runs.subscribe(host), methods=["GET"]
        ),
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
