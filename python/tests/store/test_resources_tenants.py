"""The ledger is tenant-scoped: one tenant can never see or move another's rows."""

import asyncio
from pathlib import Path

from threads.log import BranchId, ThreadId
from threads.result import Err, Ok
from threads.store import SqliteStore
from threads.store.lease import TTL_MS

ALPHA_THREAD = ThreadId("0192a000-0000-7000-8000-00000000000a")
ALPHA = BranchId("0192b000-0000-7000-8000-00000000000a")
T0 = 1_790_000_000_000


async def open_as(path: Path, tenant: str) -> SqliteStore:
    opened = await SqliteStore.open(path, tenant_id=tenant)
    assert isinstance(opened, Ok)
    return opened.value


def test_another_tenant_can_not_move_a_known_row(tmp_path: Path) -> None:
    async def main() -> None:
        path = tmp_path / "threads.db"
        alpha, beta = await open_as(path, "alpha"), await open_as(path, "beta")
        try:
            assert await alpha.create(ALPHA_THREAD, ALPHA, T0) == Ok(None)
            writer = await alpha.acquire(ALPHA, "a", lambda: T0)
            assert isinstance(writer, Ok)
            row = await alpha.ledger.pending(writer.value.owner, "fake", "sandbox", T0)
            assert isinstance(row, Ok)
            # beta knows the resource id and replays alpha's owner: every path refuses. Late
            # enough that alpha's owner lease is gone, so only the tenant scope protects the row.
            late = T0 + TTL_MS
            assert await beta.ledger.gc_resolve(row.value, "not_found", late) is None
            assert await beta.ledger.settle_release(row.value, "released", late) is None
            assert await beta.ledger.gc_retry(row.value, late) is None
            assert await beta.ledger.orphaned(late) == ()
            for refused in (
                await beta.ledger.pending(writer.value.owner, "fake", "sandbox", T0),
                await beta.ledger.resolve(writer.value.owner, row.value, "not_found", T0),
                await beta.ledger.release(writer.value.owner, row.value, T0),
            ):
                assert isinstance(refused, Err)
                assert refused.error.code == "branch_not_found"
            assert await beta.ledger.rows() == ()
            assert [r.state for r in await alpha.ledger.rows()] == ["pending"]
            # The same call from the row's own tenant does resolve it.
            assert await alpha.ledger.orphaned(late) == (row.value,)
            resolved = await alpha.ledger.gc_resolve(row.value, "not_found", late)
            assert resolved is not None
            assert resolved.state == "released"
        finally:
            await alpha.close()
            await beta.close()

    asyncio.run(main())
