"""The fake sandbox keeps the adapter contract (spec/api.json `Sandbox`, `SandboxSession`)."""

import asyncio

import pytest
from pydantic import JsonValue, ValidationError

from threads.loop.model import Found, NotFound
from threads.result import Err, Ok
from threads.sandbox import FakeSandbox, SandboxSession, fake_sandbox

NOT_FOUND_EXIT = 127


async def session(sandbox: FakeSandbox, key: str = "k1") -> SandboxSession:
    made = await sandbox.create(key)
    assert isinstance(made, Ok)
    return made.value


def test_a_malformed_script_is_refused() -> None:
    with pytest.raises(ValidationError):
        fake_sandbox({"snapshots": {"s": {"restore_sandbox_id": 1}}})
    with pytest.raises(ValidationError):
        fake_sandbox({"tools": {"t": {"output": "x", "surprise": True}}})


def test_files_keep_the_path_contract() -> None:
    async def main() -> None:
        s = await session(fake_sandbox())
        assert await s.upload("/workspace/a.txt", b"hi") == Ok(None)
        assert await s.download("/workspace/a.txt") == Ok(b"hi")
        for path, code in [
            ("workspace/a.txt", "invalid_path"),
            ("/workspace/../etc/passwd", "invalid_path"),
            ("/workspace", "is_directory"),
            ("/workspace/b.txt", "not_found"),
        ]:
            got = await s.download(path)
            assert isinstance(got, Err)
            assert got.error.code == code, path

    asyncio.run(main())


def test_exec_is_scripted_per_command_with_provider_dedup() -> None:
    script: dict[str, JsonValue] = {
        "tools": {"make": {"output": "built", "executed_keys": {"k-old": "cached"}}}
    }

    async def main() -> None:
        s = await session(fake_sandbox(script))
        for key, want in [("k-new", b"built"), ("k-old", b"cached")]:
            ran = await s.exec(["make"], process_key=key)
            assert isinstance(ran, Ok)
            assert b"".join([c async for c in ran.value.stdout]) == want
            assert await ran.value.exit_code == 0
        missing = await s.exec(["nope"], process_key="k2")
        assert isinstance(missing, Ok)
        assert await missing.value.exit_code == NOT_FOUND_EXIT
        assert await s.terminate("k-new") == Ok("terminated")
        assert await s.terminate("never-ran") == Ok("already_exited")

    asyncio.run(main())


def test_a_restored_snapshot_is_isolated_from_its_parent() -> None:
    """F11.1: a file written before the snapshot exists in the restored child; a write in the
    child is invisible to the parent."""

    async def main() -> None:
        sandbox = fake_sandbox()
        parent = await session(sandbox)
        assert await parent.upload("/workspace/a.txt", b"v1") == Ok(None)
        snap = await parent.snapshot("snap-key")
        assert isinstance(snap, Ok)
        assert await sandbox.lookup_snapshot("snap-key") == Found(snap.value)
        child = await sandbox.restore(snap.value.snapshot_id, "restore-key")
        assert isinstance(child, Ok)
        assert await sandbox.lookup("restore-key") == Found(child.value)
        assert await child.value.download("/workspace/a.txt") == Ok(b"v1")
        assert await child.value.upload("/workspace/a.txt", b"v2") == Ok(None)
        assert await parent.download("/workspace/a.txt") == Ok(b"v1")
        assert await sandbox.lookup("never-used") == NotFound()

    asyncio.run(main())


def test_a_closed_sandbox_can_not_be_reattached() -> None:
    async def main() -> None:
        sandbox = fake_sandbox()
        s = await session(sandbox)
        assert isinstance(await sandbox.attach(s.id), Ok)
        assert await s.close() == Ok(None)
        gone = await sandbox.attach(s.id)
        assert isinstance(gone, Err)
        assert gone.error.code == "not_found"

    asyncio.run(main())
