"""The fake sandbox keeps the adapter contract (spec/api.json `Sandbox`, `SandboxSession`)."""

import asyncio

import pytest
from pydantic import JsonValue, ValidationError
from sandbox_kit import OPEN, KitContext

from threads.loop.model import Found, LookupUnknown, NotFound
from threads.result import Err, Ok
from threads.sandbox import FakeSandbox, SandboxSession, fake_sandbox
from threads.store.context import CleanupAuthority

NOT_FOUND_EXIT = 127


async def session(sandbox: FakeSandbox, key: str = "k1") -> SandboxSession:
    made = await sandbox.create(key, OPEN)
    assert isinstance(made, Ok)
    return made.value


def test_a_malformed_script_is_refused() -> None:
    with pytest.raises(ValidationError):
        fake_sandbox({"snapshots": {"s": {"restore_sandbox_id": 1, "manifest": []}}})
    with pytest.raises(ValidationError):
        fake_sandbox({"snapshots": {"s": {"restore_sandbox_id": "x"}}})
    with pytest.raises(ValidationError):
        fake_sandbox({"tools": {"t": {"output": "x", "surprise": True}}})


def test_files_keep_the_path_contract() -> None:
    async def main() -> None:
        s = await session(fake_sandbox())
        assert await s.upload("/workspace/a.txt", b"hi", OPEN) == Ok(None)
        assert await s.download("/workspace/a.txt", OPEN) == Ok(b"hi")
        for path, code in [
            ("workspace/a.txt", "invalid_path"),
            ("/workspace/../etc/passwd", "invalid_path"),
            ("/workspace", "is_directory"),
            ("/workspace/b.txt", "not_found"),
        ]:
            got = await s.download(path, OPEN)
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
            ran = await s.exec(["make"], OPEN, process_key=key)
            assert isinstance(ran, Ok)
            assert b"".join([c async for c in ran.value.stdout]) == want
            assert await ran.value.exit_code == 0
        missing = await s.exec(["nope"], OPEN, process_key="k2")
        assert isinstance(missing, Ok)
        assert await missing.value.exit_code == NOT_FOUND_EXIT
        assert await s.terminate("k-new", OPEN) == Ok("terminated")
        assert await s.terminate("never-ran", OPEN) == Ok("already_exited")

    asyncio.run(main())


def test_every_provider_operation_is_fenced() -> None:
    """A stale owner reaches nothing: every operation checks its context first."""

    async def main() -> None:
        sandbox = fake_sandbox()
        s = await session(sandbox)
        stale = KitContext(live=False)
        refused = [
            await sandbox.create("k2", stale),
            await sandbox.attach(s.id, stale),
            await sandbox.release("snap", stale),
            await s.exec(["x"], stale, process_key="p"),
            await s.terminate("p", stale),
            await s.upload("/workspace/a", b"", stale),
            await s.download("/workspace/a", stale),
            await s.snapshot("k3", stale),
            await s.close(stale),
        ]
        assert all(isinstance(r, Err) and r.error.code == "stale_epoch" for r in refused)
        assert isinstance(await sandbox.lookup("k1", stale), LookupUnknown)
        assert (sandbox.creates, sandbox.releases, stale.fences) == (1, 0, 10)

    asyncio.run(main())


def test_a_lost_cleanup_claim_is_its_own_refusal() -> None:
    async def main() -> None:
        sandbox = fake_sandbox()
        s = await session(sandbox)
        lost = KitContext(live=False, authority=CleanupAuthority("res", "claim"))
        for refused in [await sandbox.release("snap", lost), await s.close(lost)]:
            assert isinstance(refused, Err)
            assert refused.error.code == "cleanup_claim_lost"
        assert sandbox.releases == 0

    asyncio.run(main())


def test_a_restored_snapshot_is_isolated_and_verified() -> None:
    """F11.1: a file written before the snapshot exists in the restored child; a write in the
    child is invisible to the parent. A wrong manifest hash creates nothing that survives."""

    async def main() -> None:
        sandbox = fake_sandbox()
        parent = await session(sandbox)
        assert await parent.upload("/workspace/a.txt", b"v1", OPEN) == Ok(None)
        snap = await parent.snapshot("snap-key", OPEN)
        assert isinstance(snap, Ok)
        assert await sandbox.lookup_snapshot("snap-key", OPEN) == Found(snap.value)
        bad = await sandbox.restore(snap.value.snapshot_id, "0" * 64, "bad-key", OPEN)
        assert isinstance(bad, Err)
        assert bad.error.code == "snapshot_manifest_mismatch"
        assert await sandbox.lookup("bad-key", OPEN) == NotFound()
        manifest = snap.value.manifest_hash
        child = await sandbox.restore(snap.value.snapshot_id, manifest, "restore-key", OPEN)
        assert isinstance(child, Ok)
        assert await sandbox.lookup("restore-key", OPEN) == Found(child.value)
        assert await child.value.download("/workspace/a.txt", OPEN) == Ok(b"v1")
        assert await child.value.upload("/workspace/a.txt", b"v2", OPEN) == Ok(None)
        assert await parent.download("/workspace/a.txt", OPEN) == Ok(b"v1")

    asyncio.run(main())


def test_a_closed_sandbox_can_not_be_reattached() -> None:
    async def main() -> None:
        sandbox = fake_sandbox()
        s = await session(sandbox)
        assert isinstance(await sandbox.attach(s.id, OPEN), Ok)
        assert await s.close(OPEN) == Ok(None)
        gone = await sandbox.attach(s.id, OPEN)
        assert isinstance(gone, Err)
        assert gone.error.code == "not_found"

    asyncio.run(main())
