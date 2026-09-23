"""Live gate for the E2B adapter: real sandboxes, a real snapshot and restore (F11.1, brief
step 1). Skipped unless THREADS_LIVE=1 and E2B_API_KEY are set; never part of the offline
suite.

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


def test_a_snapshot_restores_verified_and_isolated() -> None:
    async def body(sandbox: E2BSandbox) -> None:
        parent = await _session(sandbox)
        children: list[SandboxSession] = []
        try:
            assert await parent.upload("/workspace/a.txt", b"v1", OPEN) == Ok(None)
            snap = await parent.snapshot(str(uuid.uuid4()), OPEN)
            assert isinstance(snap, Ok), snap
            ident, manifest = snap.value.snapshot_id, snap.value.manifest_hash
            bad = await sandbox.restore(ident, "0" * 64, str(uuid.uuid4()), OPEN)
            assert isinstance(bad, Err)
            assert bad.error.code == "snapshot_manifest_mismatch"
            child = await sandbox.restore(ident, manifest, str(uuid.uuid4()), OPEN)
            assert isinstance(child, Ok), child
            children.append(child.value)
            assert await child.value.download("/workspace/a.txt", OPEN) == Ok(b"v1")
            assert await child.value.upload("/workspace/a.txt", b"v2", OPEN) == Ok(None)
            assert await parent.download("/workspace/a.txt", OPEN) == Ok(b"v1")
            assert await sandbox.release(ident, OPEN) == Ok("released")
        finally:
            for s in (parent, *children):
                assert await s.close(OPEN) == Ok(None)

    _live(body)


def test_a_detached_writer_never_yields_an_unverified_restore() -> None:
    """E2B's pause is not assumed to settle what a command detached. A writer
    that outlives its command either makes the restore fail verification or left the tree
    as the manifest says; a restore never claims a tree it doesn't have."""

    async def body(sandbox: E2BSandbox) -> None:
        parent = await _session(sandbox)
        children: list[SandboxSession] = []
        try:
            loop = "while :; do date +%s%N >> /workspace/tick; done > /dev/null 2>&1 &"
            spawned = await parent.exec(["sh", "-c", loop], OPEN, process_key="bg")
            assert isinstance(spawned, Ok), spawned
            await collect(spawned.value)
            snap = await parent.snapshot(str(uuid.uuid4()), OPEN)
            assert isinstance(snap, Ok), snap
            child = await sandbox.restore(
                snap.value.snapshot_id, snap.value.manifest_hash, str(uuid.uuid4()), OPEN
            )
            if isinstance(child, Ok):
                children.append(child.value)
            else:
                assert child.error.code == "snapshot_manifest_mismatch"
            assert await parent.terminate("bg", OPEN) == Ok("unknown")
            await sandbox.release(snap.value.snapshot_id, OPEN)
        finally:
            for s in (parent, *children):
                assert await s.close(OPEN) == Ok(None)

    _live(body)
