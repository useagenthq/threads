"""A Python host on a local port, for the TypeScript end-to-end tests that run the stock AI SDK and
AG-UI clients against it (typescript/packages/host/test/jobs/ui-python.test.ts).

Usage: python tests/host/ui_server.py '<ModelScript JSON>'. The agent `support` plays that script
with two tools: `send_email` (needs approval from alice or bob) and `lookup` (read only). The
caller is the Principal JSON in the `x-principal` header, as in the TypeScript host tests. Prints
the port once it is listening, then serves until it is stopped."""

import asyncio
import socket
import sys

import uvicorn
from pydantic import BaseModel, JsonValue, TypeAdapter
from starlette.requests import Request

from threads import RunContext, agent, scripted_model, sqlite, tool
from threads.host import host
from threads.log import Principal

ALICE = Principal(issuer="api", tenant="acme", subject="alice")
BOB = Principal(issuer="api", tenant="acme", subject="bob")


class Mail(BaseModel):
    to: str


class Query(BaseModel):
    q: str


async def send(_args: Mail, _ctx: RunContext[None]) -> str:
    return "sent"


async def look_up(_args: Query, _ctx: RunContext[None]) -> str:
    return "42"


async def authenticate(request: Request) -> Principal | None:
    header = request.headers.get("x-principal")
    return None if header is None else Principal.model_validate_json(header)


async def main(script: dict[str, JsonValue]) -> None:
    bot = agent(
        model=scripted_model(script),
        tools=[
            tool(name="send_email", description="Send an email.", input=Mail, execute=send),
            tool(
                name="lookup",
                description="Look up an answer.",
                input=Query,
                effect="read_only",
                execute=look_up,
            ),
        ],
        approvers=[ALICE, BOB],
    )
    served = host(store=sqlite(":memory:"), agents={"support": bot}, authenticate=authenticate)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    async with served:
        server = uvicorn.Server(uvicorn.Config(served.asgi, log_level="warning"))
        print(listener.getsockname()[1], flush=True)
        await server.serve(sockets=[listener])


if __name__ == "__main__":
    asyncio.run(main(TypeAdapter(dict[str, JsonValue]).validate_json(sys.argv[1])))
