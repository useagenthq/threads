"""Exec output is spilled at the source: previews for the model, the full bytes as an artifact
written a chunk at a time."""

import asyncio
from pathlib import Path

from threads.log import Spill
from threads.result import Err, Ok
from threads.sandbox import Command, ExecResult, SandboxError, fake_sandbox, run_exec
from threads.sandbox.fake_session import CHUNK
from threads.store import SqliteStore

LIMITS = Spill(threshold_bytes=100, head_bytes=10, tail_bytes=5, request_budget_bytes=1000)


async def run(output: str, store: SqliteStore) -> Ok[ExecResult] | Err[SandboxError]:
    sandbox = fake_sandbox({"tools": {"cat": {"output": output}}})
    made = await sandbox.create("k")
    assert isinstance(made, Ok)
    return await run_exec(made.value, Command(["cat"], "key-1"), await store.spill(), LIMITS)


def test_small_output_is_whole_and_not_spilled() -> None:
    async def main() -> None:
        opened = await SqliteStore.open()
        assert isinstance(opened, Ok)
        got = await run("hello", opened.value)
        assert isinstance(got, Ok)
        result = got.value
        assert (result.stdout, result.truncated, result.full_output) == ("hello", False, None)
        await opened.value.close()

    asyncio.run(main())


def test_large_output_keeps_head_and_tail_and_spills_every_byte(tmp_path: Path) -> None:
    # Several chunks, the first one already over the preview.
    output = "H" * 10 + "x" * (2 * CHUNK) + "TAIL!"

    async def main() -> None:
        opened = await SqliteStore.open(tmp_path / "threads.db")
        assert isinstance(opened, Ok)
        store = opened.value
        got = await run(output, store)
        assert isinstance(got, Ok)
        result = got.value
        assert result.truncated
        assert result.stdout.startswith("H" * 10)
        assert result.stdout.endswith("TAIL!")
        assert str(len(output)) in result.stdout
        full = result.full_output
        assert full is not None
        assert full.bytes == len(output)
        assert await store.get_artifact(full.sha256) == Ok(output.encode())
        # Nothing but the tree is left behind: the temp file became the artifact.
        assert [p.name for p in (tmp_path / "artifacts").iterdir()] == ["sha256"]
        await store.close()

    asyncio.run(main())
