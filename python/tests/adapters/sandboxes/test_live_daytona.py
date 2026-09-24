"""Live gate for Daytona: a real create, exec, file round trip, cold snapshot, verified restore
and close. Skipped unless THREADS_LIVE=1 and DAYTONA_API_KEY are set; never part of the offline
suite (AGENTS.md, Tests).

    THREADS_LIVE=1 DAYTONA_API_KEY=... uv run pytest -m live tests/adapters/sandboxes
"""

import asyncio
import os
import uuid

import aiohttp
import pytest
from sandbox_kit import OPEN

from threads.adapters.sandboxes.posix import collect
from threads.daytona import daytona
from threads.loop.model import Found
from threads.result import Ok

pytestmark = pytest.mark.live


def test_daytona_round_trip() -> None:
    if os.environ.get("THREADS_LIVE") != "1" or not os.environ.get("DAYTONA_API_KEY"):
        pytest.skip("live gate: set THREADS_LIVE=1 and DAYTONA_API_KEY")

    async def main() -> None:
        sandbox = daytona(lifetime_ms=30 * 60_000)
        op = str(uuid.uuid4())
        made = await sandbox.create(op, OPEN)
        assert isinstance(made, Ok), made
        s = made.value
        try:
            found = await sandbox.lookup(op, OPEN)
            assert isinstance(found, Ok), found
            assert isinstance(found.value, Found), found
            assert found.value.value.id == s.id
            key = os.environ["DAYTONA_API_KEY"]
            async with (
                aiohttp.ClientSession() as http,
                http.get(
                    f"https://app.daytona.io/api/sandbox/{s.id}",
                    headers={"Authorization": f"Bearer {key}"},
                ) as got,
            ):
                record = await got.json()
            assert record["public"] is False
            assert record["networkBlockAll"] is True
            assert (record["autoStopInterval"], record["autoDeleteInterval"]) == (60, 60)
            assert await s.upload("/workspace/a.txt", b"v1", OPEN) == Ok(None)
            ran = await s.exec(["printenv"], OPEN, process_key="p", env={"FOO": "bar"})
            assert isinstance(ran, Ok), ran
            assert await collect(ran.value) == (0, b"FOO=bar\n", b"")
            snap = await s.snapshot(str(uuid.uuid4()), OPEN)
            assert isinstance(snap, Ok), snap
            key = str(uuid.uuid4())
            child = await sandbox.restore(
                snap.value.snapshot_id, snap.value.manifest_hash, key, OPEN
            )
            assert isinstance(child, Ok), child
            assert await child.value.download("/workspace/a.txt", OPEN) == Ok(b"v1")
            assert await child.value.close(OPEN) == Ok(None)
            assert isinstance(await sandbox.release(snap.value.snapshot_id, OPEN), Ok)
        finally:
            await s.close(OPEN)
            await sandbox.aclose()

    asyncio.run(main())
