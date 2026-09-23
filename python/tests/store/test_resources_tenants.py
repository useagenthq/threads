"""The ledger is tenant-scoped: one tenant can never see or move another's rows."""

import asyncio
from dataclasses import replace
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
            owner = writer.value.owner
            row = await alpha.ledger.pending(owner, "fake", "sandbox", T0)
            assert isinstance(row, Ok)
            live = await alpha.ledger.resolve(owner, row.value, "found", T0, "sbx_1")
            assert isinstance(live, Ok)
            releasing = await alpha.ledger.release(owner, live.value, T0)
            assert isinstance(releasing, Ok)
            failed = await alpha.ledger.settle_release(releasing.value, "release_error", T0)
            assert failed is not None
            # beta knows the resource id and replays alpha's owner and a claim: every path
            # refuses. Late enough that alpha's owner lease is gone.
            late = T0 + TTL_MS
            forged = replace(failed, cleanup_claim="forged")
            assert await beta.ledger.claim(failed, late) is None
            assert not await beta.ledger.holds(failed.resource_id, "forged", late)
            assert await beta.ledger.settle_release(releasing.value, "released", late) is None
            assert await beta.ledger.gc_retry(forged, late) is None
            assert await beta.ledger.gc_release(forged, late) is None
            for refused in (
                await beta.ledger.pending(owner, "fake", "sandbox", T0),
                await beta.ledger.resolve(owner, row.value, "not_found", T0),
                await beta.ledger.release(owner, live.value, T0),
            ):
                assert isinstance(refused, Err)
                assert refused.error.code == "branch_not_found"
            assert await beta.ledger.rows() == ()
            assert [r.state for r in await alpha.ledger.rows()] == ["release_failed"]
            # The same claim from the row's own tenant does move it.
            claimed = await alpha.ledger.claim(failed, late)
            assert claimed is not None
            retried = await alpha.ledger.gc_retry(claimed, late)
            assert retried is not None
            assert retried.state == "releasing"
        finally:
            await alpha.close()
            await beta.close()

    asyncio.run(main())
