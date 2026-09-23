"""The Daytona adapter over a mocked Daytona on loopback (daytona_server.py): the shared sandbox
suite, the ledger's crash and takeover rules, the fork conformance cases, and its own fence."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from corpus import CASES, cases
from daytona_server import API_KEY, serve
from fork_kit import assert_expected, run_case, script_of
from sandbox_backend import FakeBackend
from sandbox_contract import CHECKS, Check, run_check
from sandbox_kit import OPEN, KitContext
from sandbox_ledger_kit import LEDGER, Body, run_ledger

from threads.adapters.sandboxes.daytona.logs import STDERR, STDOUT, Demux
from threads.agents.config import ConfigError
from threads.daytona import DaytonaSandbox, daytona
from threads.result import Err, Ok


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
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
            got = await sandbox.create("k", stale)
            await sandbox.aclose()
            await asyncio.sleep(0.05)
        assert isinstance(got, Err)
        assert got.error.code == "stale_epoch"
        assert stale.fences == 1
        assert b"".join(received) == b""

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
            got = await sandbox.attach("sbx", OPEN)
            await sandbox.aclose()
        assert isinstance(got, Err)
        assert got.error.code == "unavailable"

    asyncio.run(main())


def test_the_log_stream_splits_markers_across_frames() -> None:
    demux = Demux()
    frames = [STDOUT + b"ou", b"t" + STDERR[:2], STDERR[2:] + b"err" + STDOUT[:1], STDOUT[1:]]
    chunks = [c for f in frames for c in demux.feed(f)] + demux.flush()
    joined = {s: b"".join(c for t, c in chunks if t == s) for s in ("stdout", "stderr")}
    assert joined == {"stdout": b"out", "stderr": b"err"}


def test_egress_is_denied_unless_allowed() -> None:
    async def main() -> None:
        async with serve(FakeBackend.scripted(), "daytona") as sandbox:
            assert sandbox.info.egress == "enforced"
            assert isinstance(await sandbox.create("k", OPEN), Ok)

    asyncio.run(main())
    assert daytona(api_key=API_KEY, allow_internet=True).info.egress == "unenforced"
    with pytest.raises(ConfigError):
        daytona(api_key=API_KEY, name="Not-A-Name")


def test_a_missing_key_is_a_setup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    with pytest.raises(ConfigError) as refused:
        daytona()
    assert refused.value.code == "missing_secret"
