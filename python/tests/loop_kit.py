"""What the loop-ownership tests share: a loopback server on its own thread and event loop (so
the code under test can make and close as many loops as it likes), a scripted agent that runs
one `bash` call per run, and a recorder of what reaches the loop's exception handler."""

import asyncio
import threading
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from types import SimpleNamespace

import aiohttp
from aiohttp import web
from daytona_server import API_KEY, DaytonaServer
from pydantic import JsonValue
from sandbox_backend import FakeBackend

from threads import Agent, agent, scripted_model
from threads.adapters.loop_resources import holding
from threads.adapters.sandboxes.daytona import DaytonaSandbox
from threads.log import Permissions
from threads.sandbox.protocol import Sandbox

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
BYPASS = Permissions.model_validate(
    {
        "mode": "bypass",
        "allow": [],
        "ask": [],
        "deny": [],
        "protected_paths": [".git/**"],
        "allow_bypass": False,
        "plan_exit_mode": "default",
    }
)


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def use(name: str, args: JsonValue, call_id: str = "call_1") -> JsonValue:
    part: JsonValue = {"type": "tool_use", "call_id": call_id, "name": name, "input": args}
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


def bash_agent(sandbox: Sandbox, runs: int, command: str = "echo hi") -> Agent[None, str]:
    """An agent whose every run makes one bash call in `sandbox`, then answers."""
    replies: list[JsonValue] = []
    for n in range(runs):
        replies += [use("bash", {"command": command}, f"call_{n}"), text("Done.")]
    return agent(model=scripted_model({"responses": replies}), sandbox=sandbox, permissions=BYPASS)


@contextmanager
def served(make: Callable[[], Awaitable[web.Application]]) -> Generator[str]:
    """The app `make` builds, on loopback, served from its own thread and loop; yields its URL."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    async def start() -> tuple[web.AppRunner, str]:
        runner = web.AppRunner(await make())
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port: int = runner.addresses[0][1]
        return runner, f"http://127.0.0.1:{port}"

    runner, url = asyncio.run_coroutine_threadsafe(start(), loop).result()
    try:
        yield url
    finally:
        asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result()
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        loop.close()


class Handled:
    """Installed as a loop's exception handler: what asyncio reported there (an exception
    that was never retrieved goes to the handler, not to warnings)."""

    def __init__(self) -> None:
        self.contexts: list[dict[str, object]] = []

    def __call__(self, _loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
        self.contexts.append(context)

    def install(self) -> None:
        asyncio.get_running_loop().set_exception_handler(self)

    @property
    def messages(self) -> list[str]:
        return [str(c.get("message")) for c in self.contexts]


class Sessions:
    """Every aiohttp session the Daytona adapter sent a request on."""

    def __init__(self) -> None:
        self.seen: list[aiohttp.ClientSession] = []

    def trace(self) -> aiohttp.TraceConfig:
        async def start(session: aiohttp.ClientSession, _c: SimpleNamespace, _p: object) -> None:
            if session not in self.seen:
                self.seen.append(session)

        config = aiohttp.TraceConfig()
        config.on_request_start.append(start)
        return config


@contextmanager
def daytona(sessions: Sessions) -> Generator[DaytonaSandbox]:
    """The real Daytona adapter over a mocked Daytona served from its own thread."""
    server = DaytonaServer(FakeBackend())

    async def app() -> web.Application:
        return server.app

    with served(app) as url:
        server.base = url
        yield DaytonaSandbox(
            API_KEY, api_url=url, poll_s=0.0, wait_s=5.0, traces=[sessions.trace()]
        )


async def held[T](work: Awaitable[T]) -> T:
    """`work`, with the loop's adapter connections held as a run holds them."""
    async with holding():
        return await work
