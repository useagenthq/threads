"""Live gate for Docker: a real container on the local daemon — created, exec'd with an exact
environment and with stdin, a file round trip, a tree exported and imported back, a long
command terminated, and the container and both its volumes released.

Gated on THREADS_LIVE=1 and a reachable Engine socket; never part of the offline suite
(AGENTS.md, Tests).

    THREADS_LIVE=1 uv run pytest -m live tests/adapters/sandboxes/test_live_docker.py
"""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import pytest
from loop_kit import held
from sandbox_kit import OPEN

from threads.adapters.sandboxes.posix import collect
from threads.agents.config import ConfigError
from threads.docker import docker
from threads.loop.model import Found
from threads.result import Err, Ok
from threads.sandbox import SandboxSession, Trees

pytestmark = pytest.mark.live


def test_docker_round_trip() -> None:
    if os.environ.get("THREADS_LIVE") != "1":
        pytest.skip("live gate: set THREADS_LIVE=1 with a running Docker daemon")
    sandbox = docker()
    try:
        asyncio.run(sandbox.setup())
    except ConfigError as unreachable:
        pytest.skip(f"live gate: {unreachable.message}")

    async def main() -> None:
        op = str(uuid.uuid4())
        made = await sandbox.create(op, OPEN)
        assert isinstance(made, Ok), made
        session = made.value
        try:
            found = await sandbox.lookup(op, OPEN)
            assert isinstance(found, Ok), found
            assert isinstance(found.value, Found), found
            assert found.value.value.id == session.id
            await _exec(session)
            await _files(session)
            await _trees(session)
            await _terminated(session)
        finally:
            assert await session.close(OPEN) == Ok(None)
        gone = await sandbox.attach(session.id, OPEN)
        assert isinstance(gone, Err)
        assert gone.error.code == "not_found"

    asyncio.run(held(main()))  # the loop is held as a run holds it


async def _exec(session: SandboxSession) -> None:
    """Exactly the given environment (invariant 4: nothing of the image's own), and stdin."""
    ran = await session.exec(["printenv"], OPEN, process_key="p1", env={"FOO": "bar"})
    assert isinstance(ran, Ok), ran
    assert await collect(ran.value) == (0, b"FOO=bar\n", b"")
    fed = await session.exec(["cat"], OPEN, process_key="p2", stdin=b"from stdin")
    assert isinstance(fed, Ok), fed
    assert await collect(fed.value) == (0, b"from stdin", b"")
    whoami = await session.exec(["id", "-u"], OPEN, process_key="p3")
    assert isinstance(whoami, Ok), whoami
    assert await collect(whoami.value) == (0, b"1000\n", b"")


async def _files(session: SandboxSession) -> None:
    assert await session.upload("/workspace/a.txt", b"v1", OPEN) == Ok(None)
    assert await session.download("/workspace/a.txt", OPEN) == Ok(b"v1")
    missing = await session.download("/workspace/nope.txt", OPEN)
    assert isinstance(missing, Err)
    assert missing.error.code == "not_found"


async def _trees(session: SandboxSession) -> None:
    """The container's own tar out and back in."""
    assert isinstance(session, Trees)
    exported = await session.export_tree(OPEN)
    assert isinstance(exported, Ok), exported
    code, archive, err = await collect(exported.value)
    assert (code, err) == (0, b""), err
    assert archive

    assert await session.import_tree(_one(archive), OPEN) == Ok(None)
    assert await session.download("/workspace/a.txt", OPEN) == Ok(b"v1")


async def _one(archive: bytes) -> AsyncIterator[bytes]:
    yield archive


async def _terminated(session: SandboxSession) -> None:
    """A long command and the descendant it detached, both gone in the supervisor's sweep.

    The command announces itself first: exec returns once the daemon accepted it, but the
    supervisor writes the `running` record a moment later, just before it releases the child.
    Terminating ahead of that record is answered `unknown`, which is honest (D-3) but is not
    what this case is about, so the first byte of output is the barrier.
    """
    started = await session.exec(
        ["sh", "-c", "setsid sleep 300 & echo up; exec sleep 300"], OPEN, process_key="long"
    )
    assert isinstance(started, Ok), started
    async for chunk in started.value.stdout:
        if chunk:
            break
    assert await session.terminate("long", OPEN) == Ok("terminated")
    assert await session.terminate("never-ran", OPEN) == Ok("unknown")
    left = await session.exec(["sh", "-c", "ps -e -o user= | sort -u"], OPEN, process_key="after")
    assert isinstance(left, Ok), left
    _, users, _ = await collect(left.value)
    assert b"1000" not in users.replace(b"node", b""), users


def test_docker_enforces_the_limits_it_was_given() -> None:
    """A limit the daemon can't enforce fails the create, so a create that succeeds means the
    container really carries it (cgroup v2 reports it from inside)."""
    if os.environ.get("THREADS_LIVE") != "1":
        pytest.skip("live gate: set THREADS_LIVE=1 with a running Docker daemon")
    sandbox = docker(cpus=1.5, memory_mb=256)
    try:
        asyncio.run(sandbox.setup())
    except ConfigError as unreachable:
        pytest.skip(f"live gate: {unreachable.message}")

    async def main() -> None:
        made = await sandbox.create(str(uuid.uuid4()), OPEN)
        assert isinstance(made, Ok), made
        try:
            read = ["sh", "-c", "cat /sys/fs/cgroup/memory.max"]
            ran = await made.value.exec(read, OPEN, process_key="limit")
            assert isinstance(ran, Ok), ran
            assert await collect(ran.value) == (0, b"268435456\n", b"")
        finally:
            assert await made.value.close(OPEN) == Ok(None)

    asyncio.run(held(main()))
