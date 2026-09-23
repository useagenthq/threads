"""The host's HTTP API (spec/schema/host-api/openapi.json) as an ASGI app, on Starlette (the
`host` extra). Every /v1 route calls the library method its operation names with the
authenticated principal, scoped to that principal's tenant; `ready` and `stop` are its
lifespan."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.routing import Route

from threads.host.app import Host
from threads.host.http import runs, threads


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
    ]
    return Starlette(routes=routes, lifespan=lifespan)
