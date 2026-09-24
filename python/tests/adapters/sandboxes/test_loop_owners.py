"""The real adapters on several event loops in turn: each loop gets its own clients, and a
loop's clients are closed (exec streams ended, background stops drained first) before the loop
is gone, with no stale-loop errors and no leaked sockets (warnings are errors here)."""

import asyncio
import gc
import logging
from contextlib import AsyncExitStack
from pathlib import Path

import grpclib.client
import httpx2
import pytest
from aiohttp.test_utils import TestServer
from anthropic import AsyncAnthropic
from daytona_server import API_KEY, DaytonaServer
from e2b_fake import TracedTransport, adapter
from grpclib.testing import ChannelFor
from loop_kit import BYPASS, Sessions, text, use
from modal_fake import ROUTER_URL, TOKEN_ID, TOKEN_SECRET, FakeModal
from pydantic import JsonValue
from sandbox_backend import FakeBackend
from sandbox_deadline_kit import LIMITS
from sandbox_kit import OPEN

from threads import Completed, agent, scripted_model, sqlite
from threads.adapters import loop_resources
from threads.adapters.loop_resources import holding
from threads.adapters.models.anthropic import model as anthropic_model
from threads.adapters.sandboxes.daytona import DaytonaSandbox
from threads.adapters.sandboxes.e2b.envd import Envd, Transports
from threads.adapters.sandboxes.modal.sandbox import SERVER_URL
from threads.adapters.sandboxes.posix import collect
from threads.anthropic import anthropic
from threads.modal import modal
from threads.result import Err, Ok
from threads.sandbox import Command, run_exec
from threads.sandbox import exec as sandbox_exec
from threads.store import SqliteStore


class Envds:
    """Every Envd opened, and every one closed (its exec streams, then its clients)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.opened: list[Envd] = []
        self.closed: list[Envd] = []
        opening, closing = Envd.__init__, Envd.aclose

        def init(envd: Envd, *args: object) -> None:
            opening(envd, *args)  # pyright: ignore[reportArgumentType] - Envd's own arguments
            self.opened.append(envd)

        async def aclose(envd: Envd) -> None:
            await closing(envd)
            self.closed.append(envd)

        monkeypatch.setattr(Envd, "__init__", init)
        monkeypatch.setattr(Envd, "aclose", aclose)


def _files_and_exec(runs: int) -> list[JsonValue]:
    replies: list[JsonValue] = []
    for n in range(runs):
        replies += [
            use("write", {"path": "/workspace/a.txt", "content": "hi"}, f"w{n}"),
            use("read", {"path": "/workspace/a.txt"}, f"r{n}"),
            use("bash", {"command": "echo hi"}, f"b{n}"),
            text("Done."),
        ]
    return replies


def _closed(transports: Transports) -> bool:
    assert isinstance(transports.http, TracedTransport)
    return transports.http.closed


def test_e2b_on_two_loops_file_and_exec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    envds = Envds(monkeypatch)
    made: list[Transports] = []
    box = adapter(FakeBackend(), "e2b", made=made)
    model = scripted_model({"responses": _files_and_exec(2)})
    bot = agent(model=model, sandbox=box, permissions=BYPASS)
    for n in (1, 2):
        result = bot.run_sync("go", store=sqlite(str(tmp_path)))
        assert isinstance(result, Completed), result
        # This loop made its own transports and envd, and closed them before it ended (so
        # before the next loop's first request).
        assert len(made) == n
        assert len(envds.opened) == n
        assert envds.closed == envds.opened
        assert _closed(made[-1])
    assert made[0] is not made[1]


def test_an_e2b_close_goes_on_past_a_failing_sandbox_close(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One envd's close fails: the other envd, the control client and the transports are still
    closed, and the failure is reported once."""
    envds = Envds(monkeypatch)
    closing = Envd.aclose

    async def failing_first(envd: Envd) -> None:
        await closing(envd)
        if envd is envds.opened[0]:
            raise RuntimeError("the rpc client refused to close")

    monkeypatch.setattr(Envd, "aclose", failing_first)
    made: list[Transports] = []
    box = adapter(FakeBackend(), "e2b", made=made)

    async def main() -> None:
        async with holding():
            for key in ("k1", "k2"):
                assert isinstance(await box.create(key, OPEN), Ok)

    with caplog.at_level(logging.WARNING, "threads"):
        asyncio.run(main())
    assert len(envds.closed) == len(envds.opened)
    assert _closed(made[0])
    assert box._planes._bundles == {}  # pyright: ignore[reportPrivateUsage] - retired
    (warning,) = [r.getMessage() for r in caplog.records]
    assert "the rpc client refused to close" in warning


def test_an_e2b_exec_still_streaming_is_cancelled_at_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envds = Envds(monkeypatch)
    box = adapter(FakeBackend(), "e2b")

    async def main() -> None:
        async with holding():
            made = await box.create("k1", OPEN)
            assert isinstance(made, Ok)
            started = await made.value.exec(["sleep", "1000"], OPEN, process_key="p1")
            assert isinstance(started, Ok)  # left unread and running
            (envd,) = envds.opened
            pumps = list(envd._pumps)  # pyright: ignore[reportPrivateUsage] - its streams
            assert pumps
            assert not any(p.done() for p in pumps)
        assert all(p.done() for p in pumps)  # stopped and awaited by the release
        assert envds.closed == [envd]

    asyncio.run(main())


def test_one_modal_adapter_on_two_loops() -> None:
    """Each loop opens its own channels; the first loop's are never reused on the second."""
    backend = FakeBackend()
    served: dict[str, grpclib.client.Channel] = {}
    opened: list[str] = []

    def connect(url: str) -> grpclib.client.Channel:
        opened.append(url)
        return served[url]

    box = modal(image_id="im-test", token_id=TOKEN_ID, token_secret=TOKEN_SECRET, connect=connect)

    async def main(key: str) -> None:
        # The in-memory channels outlive the hold, as in modal_fake.harness.
        async with holding(), AsyncExitStack() as stack:
            for url in (SERVER_URL, ROUTER_URL):
                served[url] = await stack.enter_async_context(ChannelFor([FakeModal(backend)]))
            made = await box.create(key, OPEN)
            assert isinstance(made, Ok)
            ran = await made.value.exec(["echo", "hi"], OPEN, process_key=key)
            assert isinstance(ran, Ok)
            assert await collect(ran.value) == (0, b"hi\n", b"")

    asyncio.run(main("k1"))
    asyncio.run(main("k2"))
    assert opened == [SERVER_URL, ROUTER_URL] * 2


def test_an_anthropic_client_per_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[AsyncAnthropic] = []
    real = anthropic_model.client

    def recorded(*args: object) -> AsyncAnthropic:
        client = real(*args)  # pyright: ignore[reportArgumentType] - the factory's own
        made.append(client)
        return client

    monkeypatch.setattr(anthropic_model, "client", recorded)
    info = anthropic("claude-test", max_input_tokens=200_000, max_output_tokens=64).info
    model = anthropic_model.AnthropicModel(
        info, "sk-test-anthropic", http=httpx2.MockTransport(_refused)
    )

    async def one_loop() -> None:
        async with holding():
            model._sdk()  # pyright: ignore[reportPrivateUsage] - what a send would make
            model._sdk()  # pyright: ignore[reportPrivateUsage]

    asyncio.run(one_loop())
    asyncio.run(one_loop())
    assert len(made) == 2  # noqa: PLR2004 - one client per loop
    assert all(c.is_closed() for c in made)


def _refused(_request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(500)


def test_release_to_zero_with_two_adapters_on_one_loop() -> None:
    """Two holds on one loop: the first to end closes nothing; the last closes both adapters'
    clients and removes the loop's entry."""
    sessions = Sessions()
    made: list[Transports] = []
    e2b_box = adapter(FakeBackend(), "e2b", made=made)

    async def main() -> None:
        server = DaytonaServer(FakeBackend())
        async with TestServer(server.app, host="127.0.0.1") as test:
            server.base = str(test.make_url("")).rstrip("/")
            daytona_box = DaytonaSandbox(
                API_KEY, api_url=server.base, poll_s=0.0, wait_s=5.0, traces=[sessions.trace()]
            )
            first_done = asyncio.Event()

            async def first() -> None:
                async with holding():
                    assert isinstance(await daytona_box.create("d1", OPEN), Ok)
                    assert isinstance(await e2b_box.create("e1", OPEN), Ok)
                first_done.set()

            async def second() -> None:
                async with holding():
                    await first_done.wait()
                    (session,) = sessions.seen
                    assert not session.closed
                    assert not _closed(made[0])  # still held

            await asyncio.gather(second(), first())
            assert sessions.seen[0].closed
            assert _closed(made[0])
            assert loop_resources._entries == {}  # pyright: ignore[reportPrivateUsage]

    asyncio.run(main())


@pytest.mark.parametrize("kill_s", [0.05, 5.0], ids=["stop_finishes", "stop_is_cancelled"])
def test_a_daytona_stop_in_flight_is_drained_before_the_session_closes(kill_s: float) -> None:
    """The real exec timeout path: the stop starts in the background and the hold ends at
    once. Release waits for the stop, or cancels it after the grace, before the session
    closes, and no socket is left open."""
    assert not hasattr(sandbox_exec, "_stopping")

    async def main() -> None:
        server = DaytonaServer(FakeBackend())
        server.kill_delay_s = kill_s
        opened = await SqliteStore.open()
        assert isinstance(opened, Ok)
        async with TestServer(server.app, host="127.0.0.1") as test:
            server.base = str(test.make_url("")).rstrip("/")
            box = DaytonaSandbox(API_KEY, api_url=server.base, poll_s=0.0, wait_s=5.0)
            async with holding():
                made = await box.create("k1", OPEN)
                assert isinstance(made, Ok)
                command = Command(["sleep", "1000"], "p1", timeout_ms=50)
                ran = await run_exec(made.value, command, OPEN, await opened.value.spill(), LIMITS)
                assert isinstance(ran, Err)
                assert ran.error.code == "timeout"
            # Short stop: answered before the session closed. Long: cancelled at the grace.
            assert len(server.kills) == (1 if kill_s < loop_resources.GRACE_S else 0)
            await asyncio.sleep(0.05)
            assert test.runner is not None
            assert test.runner.server is not None
            assert test.runner.server.connections == [], "a socket leaked"
        await opened.value.close()

    asyncio.run(main())
    gc.collect()
