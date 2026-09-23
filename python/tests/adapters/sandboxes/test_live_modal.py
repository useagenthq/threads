"""Live gate for the Modal adapter: one real sandbox. Skipped unless THREADS_LIVE=1 and Modal
credentials and a built image are set; never part of the offline suite.

    THREADS_LIVE=1 MODAL_TOKEN_ID=... MODAL_TOKEN_SECRET=... THREADS_LIVE_MODAL_IMAGE=im-... \\
    uv run pytest -m live tests/adapters/sandboxes/test_live_modal.py
"""

import asyncio
import os
import uuid

import pytest
from sandbox_kit import OPEN

from threads.adapters.sandboxes.posix import collect
from threads.modal import modal
from threads.result import Ok

pytestmark = pytest.mark.live


def test_a_real_sandbox_runs_a_command_and_keeps_a_file() -> None:
    if os.environ.get("THREADS_LIVE") != "1":
        pytest.skip("live gate: set THREADS_LIVE=1 to reach Modal")
    needed = ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "THREADS_LIVE_MODAL_IMAGE")
    missing = [n for n in needed if not os.environ.get(n)]
    if missing:
        pytest.skip(f"live gate: {', '.join(missing)} not set")

    async def main() -> None:
        sandbox = modal(image_id=os.environ["THREADS_LIVE_MODAL_IMAGE"], lifetime_ms=600_000)
        made = await sandbox.create(str(uuid.uuid4()), OPEN)
        assert isinstance(made, Ok), made
        session = made.value
        try:
            ran = await session.exec(["echo", "hi"], OPEN, process_key="live-1")
            assert isinstance(ran, Ok), ran
            assert await collect(ran.value) == (0, b"hi\n", b"")
            assert await session.upload("/workspace/a.txt", b"v1", OPEN) == Ok(None)
            assert await session.download("/workspace/a.txt", OPEN) == Ok(b"v1")
        finally:
            assert await session.close(OPEN) == Ok(None)
            await sandbox.aclose()

    asyncio.run(main())
