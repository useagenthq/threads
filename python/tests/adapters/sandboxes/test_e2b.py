"""The E2B adapter over its mocked transports (e2b_fake.py): the shared sandbox suite, the
ledger's crash and takeover rules, the fork conformance cases, and E2B's own boundaries."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
import pytest
from corpus import CASES, cases
from e2b_fake import API_KEY, adapter, control, make
from fork_kit import assert_expected, assert_restore_refused, reaches_restore, run_case, script_of
from sandbox_backend import FakeBackend
from sandbox_contract import CHECKS, Check, run_check
from sandbox_deadline_kit import DEADLINE
from sandbox_kit import OPEN, KitContext
from sandbox_ledger_kit import LEDGER, Body, run_ledger

from threads.adapters.sandboxes import fence
from threads.adapters.sandboxes.e2b.transport import FencedHttpx, http_transport
from threads.loop.model import LookupUnknown
from threads.result import Err, Ok


@pytest.mark.parametrize("check", [*CHECKS, *DEADLINE], ids=lambda c: c.__name__)
def test_contract(check: Check) -> None:
    asyncio.run(run_check(check, make, (API_KEY,)))


@pytest.mark.parametrize("body", LEDGER, ids=lambda b: b.__name__)
def test_ledger(body: Body) -> None:
    asyncio.run(run_ledger(body, make))


@pytest.mark.parametrize("name", cases("fork"))
def test_fork_case(name: str) -> None:
    case = CASES / name

    async def main() -> None:
        backend = FakeBackend.scripted(script_of(case))
        async with make(backend, "fake") as sandbox:
            got = await run_case(case, sandbox, lambda: backend.creates)
        if reaches_restore(name):
            assert_restore_refused(got)  # E2B declares no snapshots
        else:
            assert_expected(name, got)

    asyncio.run(main())


def test_invalid_provider_responses_are_typed_failures() -> None:
    """A control plane that answers garbage establishes nothing: unavailable, never a crash."""

    def garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"unexpected": True})

    async def main() -> None:
        sandbox = adapter(FakeBackend.scripted(), "e2b", garbage)
        made = await sandbox.create("k", OPEN)
        assert isinstance(made, Err)
        assert made.error.code == "unavailable"
        assert isinstance(await sandbox.lookup("k", OPEN), LookupUnknown)

    asyncio.run(main())


def test_an_unfenced_transport_is_an_adapter_bug() -> None:
    """A transport that writes without httpcore's header trace would skip the fence: the
    adapter refuses to use its answer rather than send unfenced silently."""

    async def main() -> None:
        backend = FakeBackend.scripted()
        silent = httpx.MockTransport(control(backend))
        client = httpx.AsyncClient(transport=FencedHttpx(silent))
        with pytest.raises(fence.UnfencedRequestError):
            await fence.dispatch(
                OPEN, lambda: client.get("https://api.e2b.test/sandboxes/x"), lambda e: None
            )

    asyncio.run(main())


@asynccontextmanager
async def _loopback(received: list[bytes]) -> AsyncGenerator[int]:
    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received.append(await reader.read(65536))
        writer.write(b"HTTP/1.1 404 Not Found\r\ncontent-length: 2\r\n\r\n{}")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


def test_the_fence_holds_at_the_real_send_point() -> None:
    """Over a real httpcore connection: a stale owner's request writes no byte."""

    async def main() -> None:
        received: list[bytes] = []
        async with _loopback(received) as port:
            client = httpx.AsyncClient(transport=FencedHttpx(http_transport()))
            url = f"http://127.0.0.1:{port}/sandboxes/x"
            stale = KitContext(live=False)
            refused = await fence.dispatch(stale, lambda: client.get(url), lambda e: None)
            assert isinstance(refused, Err)
            assert refused.error.code == "stale_epoch"
            passed = await fence.dispatch(OPEN, lambda: client.get(url), lambda e: None)
            assert isinstance(passed, Ok)
            await client.aclose()
        # The refused request opened a connection and wrote nothing to it.
        assert [r for r in received if r] == [received[-1]]
        assert received[-1].startswith(b"GET /sandboxes/x")
        assert stale.fences == 1

    asyncio.run(main())
