"""Live gate for the E2B adapter: a real sandbox (exec, env, stdin, files). Skipped unless
THREADS_LIVE=1 and E2B_API_KEY are set; never part of the offline suite.

    THREADS_LIVE=1 E2B_API_KEY=... uv run pytest -m live tests/adapters/sandboxes/test_live_e2b.py
"""

import asyncio
import os
import uuid
from collections.abc import Callable, Coroutine

import pytest
from sandbox_kit import OPEN

from threads.adapters.sandboxes.posix import collect
from threads.e2b import E2BSandbox, e2b
from threads.result import Err, Ok
from threads.sandbox import SandboxSession

pytestmark = pytest.mark.live


def _live(body: Callable[[E2BSandbox], Coroutine[None, None, None]]) -> None:
    if os.environ.get("THREADS_LIVE") != "1":
        pytest.skip("live gate: set THREADS_LIVE=1 to reach E2B")
    if not os.environ.get("E2B_API_KEY"):
        pytest.skip("live gate: E2B_API_KEY not set")
    asyncio.run(body(e2b(lifetime_ms=600_000)))


async def _session(sandbox: E2BSandbox) -> SandboxSession:
    made = await sandbox.create(str(uuid.uuid4()), OPEN)
    assert isinstance(made, Ok), made
    return made.value


def test_exec_env_and_files() -> None:
    async def body(sandbox: E2BSandbox) -> None:
        s = await _session(sandbox)
        try:
            ran = await s.exec(["printenv"], OPEN, process_key="p1", env={"FOO": "bar"})
            assert isinstance(ran, Ok), ran
            assert await collect(ran.value) == (0, b"FOO=bar\n", b"")
            fed = await s.exec(["cat"], OPEN, process_key="p2", stdin=b"in")
            assert isinstance(fed, Ok), fed
            assert await collect(fed.value) == (0, b"in", b"")
            assert await s.upload("/workspace/a.txt", b"v1", OPEN) == Ok(None)
            assert await s.download("/workspace/a.txt", OPEN) == Ok(b"v1")
            missing = await s.download("/workspace/none", OPEN)
            assert isinstance(missing, Err)
            assert missing.error.code == "not_found"
        finally:
            assert await s.close(OPEN) == Ok(None)

    _live(body)
