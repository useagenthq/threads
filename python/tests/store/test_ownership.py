"""Who may touch a stored branch: its writer implementation and its tenant."""

import asyncio
import json
from pathlib import Path

import pytest

from threads import VERSION
from threads.log import BranchId, ThreadId
from threads.log.digest import sha256_hex
from threads.result import Err, Ok
from threads.store import ForkRequest, SqliteStore, verify_export
from threads.store.lines import head_line, header_line

THREAD = ThreadId("0192a000-0000-7000-8000-000000000001")
ROOT = BranchId("0192b000-0000-7000-8000-000000000001")
CHILD = BranchId("0192b000-0000-7000-8000-000000000002")
T0 = 1_790_000_000_000
API_SCHEMA = Path(__file__).resolve().parents[3] / "spec" / "schema" / "api.schema.json"


def root_export(old: bytes, new: bytes) -> bytes:
    """An empty root branch whose header has `old` replaced by `new`."""
    header = header_line(THREAD, ROOT, T0).replace(old, new)
    return header + b"\n" + head_line(ROOT, 0, sha256_hex(header)) + b"\n"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (b'"impl":"threads-py"', b'"impl":"threads-ts"'),
        (f'"version":"{VERSION}"'.encode(), b'"version":"99.0.0"'),
    ],
)
def test_another_writer_reads_but_never_acquires(old: bytes, new: bytes) -> None:
    export = root_export(old, new)
    verified = verify_export(export, T0)
    assert isinstance(verified, Ok)

    async def main() -> None:
        opened = await SqliteStore.open()
        assert isinstance(opened, Ok)
        store = opened.value
        try:
            assert await store.import_log(verified.value) == Ok(None)
            assert isinstance(await store.read(ROOT, T0), Ok)
            refused = await store.acquire(ROOT, "a", lambda: T0)
            assert isinstance(refused, Err)
            assert (refused.error.code, refused.error.seq) == ("writer_mismatch", 0)
        finally:
            await store.close()

    asyncio.run(main())


def test_branch_not_found_is_an_api_error_code() -> None:
    schema = json.loads(API_SCHEMA.read_text(encoding="utf-8"))
    assert "branch_not_found" in schema["$defs"]["ApiErrorCode"]["enum"]


def test_another_tenants_branch_is_not_found(tmp_path: Path) -> None:
    path = tmp_path / "threads.db"

    async def main() -> None:
        acme = await SqliteStore.open(path, tenant_id="acme")
        assert isinstance(acme, Ok)
        assert await acme.value.create(THREAD, ROOT, T0) == Ok(None)
        assert isinstance(await acme.value.export(ROOT), Ok)
        await acme.value.close()
        local = await SqliteStore.open(path)
        assert isinstance(local, Ok)
        store = local.value
        try:
            fork = ForkRequest(ROOT, 0, CHILD, {"reason": "repair"})
            results = [
                await store.read(ROOT, T0),
                await store.export(ROOT),
                await store.acquire(ROOT, "a", lambda: T0),
                await store.fork(fork, "a", lambda: T0),
                await store.read(CHILD, T0),
            ]
            for result in results:
                assert isinstance(result, Err)
                assert (result.error.code, result.error.seq) == ("branch_not_found", None)
        finally:
            await store.close()

    asyncio.run(main())


def test_importing_another_tenants_thread_is_branch_exists(tmp_path: Path) -> None:
    """A thread belongs to one tenant: a new branch of it from another tenant is refused as a
    value, without naming the owner."""
    path = tmp_path / "threads.db"
    header = header_line(THREAD, CHILD, T0)
    verified = verify_export(header + b"\n" + head_line(CHILD, 0, sha256_hex(header)) + b"\n", T0)
    assert isinstance(verified, Ok)

    async def main() -> None:
        acme = await SqliteStore.open(path, tenant_id="acme")
        assert isinstance(acme, Ok)
        assert await acme.value.create(THREAD, ROOT, T0) == Ok(None)
        await acme.value.close()
        local = await SqliteStore.open(path)
        assert isinstance(local, Ok)
        try:
            refused = await local.value.import_log(verified.value)
            assert isinstance(refused, Err)
            assert refused.error.code == "branch_exists"
            assert "acme" not in refused.error.message
            assert isinstance(await local.value.read(CHILD, T0), Err)
        finally:
            await local.value.close()

    asyncio.run(main())
