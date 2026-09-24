"""host(telemetry=...): the exporter syncs beside the scheduler, once more on stop(), and never
holds up a run; failures and skipped branches are logged once per streak."""

import asyncio
import contextlib
import logging
import time
from pathlib import Path

import pytest
from otel_collector_kit import Collector, collector
from otel_store_kit import looping

from threads import Completed, sqlite
from threads.host import host
from threads.host.telemetry import Telemetry, Timing
from threads.log import BranchId, ThreadId
from threads.otel import otel
from threads.result import Err, Ok
from threads.telemetry import SkippedBranch, SyncError, SyncReport


async def _until(c: Collector, count: int, within_s: float = 5.0) -> None:
    deadline = time.monotonic() + within_s
    while len(c.span_ids()) < count:
        assert time.monotonic() < deadline, c.span_ids()
        await asyncio.sleep(0.05)


def test_a_host_exports_on_its_tick(tmp_path: Path) -> None:
    async def main() -> None:
        async with collector() as c:
            store = sqlite(str(tmp_path / "s"))
            bot = looping(2)
            served = host(store=store, agents={"bot": bot}, telemetry=otel(endpoint=c.url))
            await served.ready()
            try:
                assert isinstance(await bot.run("go", store=store), Completed)
                await _until(c, 4)
            finally:
                await served.stop()

    asyncio.run(main())


def test_stop_runs_one_last_sync(tmp_path: Path) -> None:
    async def main() -> None:
        async with collector() as c:
            store = sqlite(str(tmp_path / "s"))
            bot = looping(2)
            served = host(store=store, agents={"bot": bot}, telemetry=otel(endpoint=c.url))
            await served.ready()
            assert isinstance(await bot.run("go", store=store), Completed)
            await served.stop()
            assert len(c.span_ids()) == 4  # noqa: PLR2004 - counted spans

    asyncio.run(main())


def test_a_collector_that_never_answers_holds_up_no_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "300")

    async def main() -> None:
        async with collector() as c:
            c.hang = True
            store = sqlite(str(tmp_path / "s"))
            served = host(store=store, agents={"bot": looping(3)}, telemetry=otel(endpoint=c.url))
            await served.ready()
            started = time.monotonic()
            for _ in range(3):
                assert isinstance(await looping(3).run("go", store=store), Completed)
                await asyncio.sleep(0.5)  # ticks run against the hanging collector meanwhile
            await served.stop()
            assert time.monotonic() - started < 10  # noqa: PLR2004 - well under any hang
            assert c.span_ids() == []

    asyncio.run(main())


class _Scripted:
    """An Exporter whose sync results are scripted."""

    def __init__(self, results: list[Ok[SyncReport] | Err[SyncError]]) -> None:
        self.results = results
        self.calls = 0

    async def sync(self) -> Ok[SyncReport] | Err[SyncError]:
        self.calls += 1
        return self.results.pop(0) if self.results else Ok(SyncReport(0, 0, ()))


BRANCH = BranchId("0192b000-0000-7000-8000-000000000002")
THREAD = ThreadId("0192a000-0000-7000-8000-0000000000c1")
FAST = Timing(every_s=0.001, max_wait_s=0.002, last_sync_s=0.5)


def _skipped(head: int) -> Ok[SyncReport]:
    return Ok(SyncReport(0, 0, (SkippedBranch(BRANCH, THREAD, "log_corrupt", head),)))


def test_a_skipped_branch_is_logged_once_per_streak(caplog: pytest.LogCaptureFixture) -> None:
    async def main() -> None:
        stub = _Scripted([*(_skipped(5) for _ in range(5)), _skipped(6), _skipped(6)])
        telemetry = Telemetry(stub, FAST)
        running = asyncio.get_running_loop().create_task(telemetry.run())
        while stub.results:
            await asyncio.sleep(0.005)
        running.cancel()

    with caplog.at_level(logging.WARNING, logger="threads.host.telemetry"):
        asyncio.run(main())
    logged = [r.getMessage() for r in caplog.records if "skipped branch" in r.getMessage()]
    # Once for head 5, once more after the head moved to 6.
    assert len(logged) == 2  # noqa: PLR2004 - counted log lines
    assert all(BRANCH in m and "log_corrupt" in m for m in logged)


def test_a_failing_collector_is_logged_once_per_streak(caplog: pytest.LogCaptureFixture) -> None:
    down = Err(SyncError("collector_unavailable", "the collector is unavailable: no answer"))
    ok = Ok(SyncReport(1, 0, ()))

    async def main() -> None:
        stub = _Scripted([down, down, down, ok, down, down])
        telemetry = Telemetry(stub, FAST)
        running = asyncio.get_running_loop().create_task(telemetry.run())
        while stub.results:
            await asyncio.sleep(0.005)
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running
        before = stub.calls
        await telemetry.last()
        assert stub.calls == before + 1  # the last sync ran too

    with caplog.at_level(logging.WARNING, logger="threads.host.telemetry"):
        asyncio.run(main())
    logged = [r for r in caplog.records if "collector_unavailable" in r.getMessage()]
    assert len(logged) == 2  # noqa: PLR2004 - counted spans
