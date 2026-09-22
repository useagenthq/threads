"""Re-importing onto a stored leaf compares its head evidence too; recovery flags open requests."""

import asyncio
from pathlib import Path

import pytest

from threads.log import BranchId, ThreadId
from threads.log.digest import sha256_hex
from threads.result import Err, Ok
from threads.store import SqliteStore, verify_export
from threads.store.lines import head_line, header_line

THREAD = ThreadId("0192a000-0000-7000-8000-000000000001")
ROOT = BranchId("0192b000-0000-7000-8000-000000000001")
T0 = 1_790_000_000_000
HEADER = header_line(THREAD, ROOT, T0) + b"\n"
VERIFIED = HEADER + head_line(ROOT, 0, sha256_hex(HEADER[:-1])) + b"\n"
CASES = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "cases"


async def reimport(first: bytes, second: bytes) -> str:
    opened = await SqliteStore.open()
    assert isinstance(opened, Ok)
    store = opened.value
    try:
        for export in (first, second):
            verified = verify_export(export, T0)
            assert isinstance(verified, Ok)
            result = await store.import_log(verified.value)
        return "ok" if isinstance(result, Ok) else result.error.code
    finally:
        await store.close()


@pytest.mark.parametrize(
    ("first", "second", "code"),
    [
        (HEADER + b"AAA", HEADER + b"AAA", "ok"),
        (VERIFIED, VERIFIED, "ok"),
        (HEADER, HEADER, "ok"),
        (HEADER + b"AAA", HEADER + b"BBB", "seq_conflict"),
        (VERIFIED, HEADER, "seq_conflict"),
        (HEADER, HEADER + b"AAA", "seq_conflict"),
    ],
)
def test_reimport_compares_head_evidence(first: bytes, second: bytes, code: str) -> None:
    assert asyncio.run(reimport(first, second)) == code


def test_an_awaited_model_request_requires_recovery() -> None:
    export = (CASES / "model-response-recovered-by-lookup" / "log.threads-py.jsonl").read_bytes()
    verified = verify_export(export, T0)
    assert isinstance(verified, Ok)
    branch = verified.value.segments[-1].header.branch_id

    async def main() -> bool:
        opened = await SqliteStore.open()
        assert isinstance(opened, Ok)
        try:
            assert await opened.value.import_log(verified.value) == Ok(None)
            writer = await opened.value.acquire(branch, "a", lambda: T0)
            assert not isinstance(writer, Err)
            return writer.value.requires_recovery
        finally:
            await opened.value.close()

    assert asyncio.run(main()) is True
