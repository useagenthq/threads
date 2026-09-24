"""The Daytona adapter over a mocked Daytona on loopback (daytona_server.py): the shared sandbox
suite, the ledger's crash and takeover rules, the fork conformance cases, and its own fence."""

import asyncio
import base64
import binascii
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web
from aiohttp._websocket.writer import WebSocketWriter
from aiohttp.test_utils import TestServer
from corpus import CASES, cases
from daytona_server import API_KEY, DaytonaServer, serve
from fork_kit import assert_expected, run_case, script_of
from sandbox_backend import FakeBackend
from sandbox_contract import CHECKS, Check, run_check
from sandbox_deadline_kit import DEADLINE
from sandbox_kit import OPEN, KitContext
from sandbox_ledger_kit import LEDGER, Body, run_ledger

from threads.adapters.loop_resources import holding
from threads.adapters.sandboxes.daytona.logs import STDERR, STDOUT, Demux, Stream, Unbase64
from threads.adapters.sandboxes.posix import collect
from threads.agents.config import ConfigError
from threads.daytona import DaytonaSandbox, daytona
from threads.loop.model import Found
from threads.result import Err, Ok


@pytest.mark.parametrize("check", [*CHECKS, *DEADLINE], ids=lambda c: c.__name__)
def test_contract(check: Check) -> None:
    asyncio.run(run_check(check, serve, (API_KEY,)))


@pytest.mark.parametrize("body", LEDGER, ids=lambda b: b.__name__)
def test_ledger(body: Body) -> None:
    asyncio.run(run_ledger(body, serve))


@pytest.mark.parametrize("name", cases("fork"))
def test_fork_case(name: str) -> None:
    async def main() -> None:
        backend = FakeBackend.scripted(script_of(CASES / name))
        async with serve(backend, "fake") as sandbox:
            assert_expected(name, await run_case(CASES / name, sandbox, lambda: backend.creates))

    asyncio.run(main())


@asynccontextmanager
async def raw(received: list[bytes]) -> AsyncGenerator[str]:
    """A loopback socket that records every byte a connection writes."""

    async def serve_raw(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(await reader.read(65536))
        writer.close()

    listening = await asyncio.start_server(serve_raw, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{listening.sockets[0].getsockname()[1]}"
    finally:
        listening.close()
        await listening.wait_closed()


def test_a_refused_fence_writes_no_byte() -> None:
    """The fence runs once the pool hands out a connection, before the request is written."""

    async def main() -> None:
        received: list[bytes] = []
        async with raw(received) as url:
            sandbox = DaytonaSandbox(API_KEY, api_url=url, poll_s=0.0, wait_s=1.0)
            stale = KitContext(live=False)
            async with holding():  # released: the adapter's session closes
                got = await sandbox.create("k", stale)
            await asyncio.sleep(0.05)
        assert isinstance(got, Err)
        assert got.error.code == "stale_epoch"
        assert stale.fences == 1
        assert b"".join(received) == b""

    asyncio.run(main())


def test_a_refused_fence_after_a_request_leaves_no_socket_open() -> None:
    """aiohttp never closes a pooled connection whose reuse hook raised, so connections are not
    reused: after a refusal right after a request, closing the adapter leaves no socket open."""

    async def main() -> None:
        server = DaytonaServer(FakeBackend.scripted())
        async with TestServer(server.app, host="127.0.0.1") as test:
            server.base = str(test.make_url("")).rstrip("/")
            sandbox = DaytonaSandbox(API_KEY, api_url=server.base, poll_s=0.0, wait_s=1.0)
            async with holding():  # released: the adapter's session closes
                assert isinstance(await sandbox.create("k", OPEN), Ok)
                for _ in range(3):
                    refused = await sandbox.attach("sbx_1", KitContext(live=False))
                    assert isinstance(refused, Err)
                    assert refused.error.code == "stale_epoch"
            await asyncio.sleep(0.05)
            assert test.runner is not None
            assert test.runner.server is not None
            assert test.runner.server.connections == [], "a socket leaked"

    asyncio.run(main())


def test_closing_the_adapter_stops_every_stream_it_is_reading() -> None:
    """An exec still streaming when the adapter closes makes no request after it: its pump is
    stopped, not left to reconnect to a provider that went away."""

    async def main() -> None:
        async with serve(FakeBackend.scripted(), "daytona") as sandbox:
            made = await sandbox.create("k", OPEN)
            assert isinstance(made, Ok)
            started = await made.value.exec(["sleep", "100"], OPEN, process_key="bg")
            assert isinstance(started, Ok)
        others = asyncio.all_tasks() - {asyncio.current_task()}
        assert not [t for t in others if not t.done()]

    asyncio.run(main())


def test_an_invalid_provider_answer_is_a_typed_error() -> None:
    async def garbage(_request: web.Request) -> web.Response:
        return web.json_response({"id": 7, "state": "started"})

    async def main() -> None:
        app = web.Application()
        app.router.add_get("/sandbox/{ref}", garbage)
        async with TestServer(app, host="127.0.0.1") as test:
            url = str(test.make_url("")).rstrip("/")
            sandbox = DaytonaSandbox(API_KEY, api_url=url, poll_s=0.0, wait_s=1.0)
            async with holding():
                got = await sandbox.attach("sbx", OPEN)
        assert isinstance(got, Err)
        assert got.error.code == "unavailable"

    asyncio.run(main())


def test_the_log_stream_splits_markers_across_frames() -> None:
    demux = Demux()
    frames = [STDOUT + b"ou", b"t" + STDERR[:2], STDERR[2:] + b"err" + STDOUT[:1], STDOUT[1:]]
    chunks = [c for f in frames for c in demux.feed(f)] + demux.flush()
    joined = {s: b"".join(c for t, c in chunks if t == s) for s in ("stdout", "stderr")}
    assert joined == {"stdout": b"out", "stderr": b"err"}


def test_framed_streams_decode_every_byte_split_anywhere() -> None:
    """base64 framing survives the channel's markers and any frame split."""
    payload = STDOUT + STDERR + bytes(range(256))
    wire = STDOUT + base64.encodebytes(payload) + STDERR + base64.encodebytes(payload[::-1])
    for size in (1, 2, 3, 5, 7, 64):
        demux = Demux()
        decoded: dict[Stream, Unbase64] = {"stdout": Unbase64(), "stderr": Unbase64()}
        got: dict[Stream, bytes] = {"stdout": b"", "stderr": b""}
        frames = [wire[i : i + size] for i in range(0, len(wire), size)]
        for stream, chunk in [c for f in frames for c in demux.feed(f)] + demux.flush():
            got[stream] += decoded[stream].feed(chunk)
        assert got == {"stdout": payload, "stderr": payload[::-1]}, size


@pytest.mark.parametrize("text", [b"QUJ", b"QU=D", b"QUJD!!!!"])
def test_malformed_framing_is_refused(text: bytes) -> None:
    def decode() -> None:
        decoder = Unbase64()
        decoder.feed(text)
        decoder.feed(b"QUJD")
        decoder.end()

    with pytest.raises(binascii.Error):
        decode()


def test_egress_is_denied_unless_allowed() -> None:
    async def main() -> None:
        async with serve(FakeBackend.scripted(), "daytona") as sandbox:
            assert sandbox.info.egress == "enforced"
            assert isinstance(await sandbox.create("k", OPEN), Ok)

    asyncio.run(main())
    assert daytona(api_key=API_KEY, allow_internet=True).info.egress == "unenforced"
    with pytest.raises(ConfigError):
        daytona(api_key=API_KEY, name="Not-A-Name")


def test_a_sandbox_is_private_and_cannot_outlive_a_leak() -> None:
    """Explicitly private; a finite idle auto-stop, then auto-delete: the net under the ledger."""

    async def main(minutes: int | None) -> list[tuple[bool, int, int]]:
        server = DaytonaServer(FakeBackend.scripted())
        async with TestServer(server.app, host="127.0.0.1") as test:
            server.base = str(test.make_url("")).rstrip("/")
            sandbox = DaytonaSandbox(API_KEY, api_url=server.base, poll_s=0.0, wait_s=1.0)
            if minutes is not None:
                sandbox = DaytonaSandbox(
                    API_KEY, api_url=server.base, poll_s=0.0, wait_s=1.0, auto_stop_minutes=minutes
                )
            async with holding():
                assert isinstance(await sandbox.create("k", OPEN), Ok)
        return [(c.public, c.auto_stop_interval, c.auto_delete_interval) for c in server.created]

    assert asyncio.run(main(None)) == [(False, 60, 60)]
    assert asyncio.run(main(5)) == [(False, 5, 5)]
    for bad in (0, -1):
        with pytest.raises(ConfigError) as refused:
            daytona(api_key=API_KEY, auto_stop_minutes=bad)
        assert refused.value.code == "invalid_config"


def test_a_failed_create_leaves_a_sandbox_lookup_finds_and_a_retry_reuses() -> None:
    """Live: the toolbox user couldn't make /workspace, so create reported failure with the
    sandbox running. Its name finds it, and a retry under the key creates nothing new."""

    async def main() -> None:
        backend = FakeBackend.scripted()
        server = DaytonaServer(backend)
        server.fail_prepares = 1
        async with TestServer(server.app, host="127.0.0.1") as test:
            server.base = str(test.make_url("")).rstrip("/")
            sandbox = DaytonaSandbox(API_KEY, api_url=server.base, poll_s=0.0, wait_s=1.0)
            async with holding():
                await _retried(sandbox, backend)

    asyncio.run(main())


async def _retried(sandbox: DaytonaSandbox, backend: FakeBackend) -> None:
    failed = await sandbox.create("k", OPEN)
    assert isinstance(failed, Err)
    assert failed.error.code == "unavailable"
    found = await sandbox.lookup("k", OPEN)
    assert isinstance(found, Ok)
    assert isinstance(found.value, Found)
    again = await sandbox.create("k", OPEN)
    assert isinstance(again, Ok)
    assert again.value.id == found.value.value.id
    assert backend.creates == 1
    ran = await again.value.exec(["echo", "hi"], OPEN, process_key="p")
    assert isinstance(ran, Ok)
    assert await collect(ran.value) == (0, b"hi\n", b"")


def test_a_log_stream_the_proxy_drops_after_its_close_frame_ended_normally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live: the proxy sends close 1000 and drops the TLS connection at once, so aiohttp can't
    write its answer and reports 1006. The peer's own close frame is what says it ended."""

    async def unanswerable(*_args: object, **_kwargs: object) -> None:
        raise aiohttp.ClientConnectionResetError("Cannot write to closing transport")

    monkeypatch.setattr(WebSocketWriter, "close", unanswerable)

    async def main() -> None:
        async with serve(FakeBackend.scripted(), "daytona") as sandbox:
            made = await sandbox.create("k", OPEN)
            assert isinstance(made, Ok), made
            ran = await made.value.exec(["echo", "hi"], OPEN, process_key="p")
            assert isinstance(ran, Ok)
            assert await collect(ran.value) == (0, b"hi\n", b"")

    asyncio.run(main())
