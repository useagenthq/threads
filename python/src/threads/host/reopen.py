"""API runs a crash left open, resumed by a starting host and looked at again until each closes or
parks (spec/schema/README.md, "API run recovery"); and each second, every branch with a
background child still to report (pending_wakes), run on so the child reports and wakes its lead
(Gate 1 §2.7.3).

A look that loses to another host's lease is tried again each second. A store error (the store
raises `StoreError`, wherever it met SQLite) is tried again with backoff while it lasts, said
once per streak. Anything else a recovery run meets is not retried: a failure is logged with its
reason and the turn stays open for a control, a new input or the next start. A provider's lookup
failing never reaches here: the loop settles it (a model request becomes unknown, an effect in
doubt parks).
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from threads.agents.results import Failed
from threads.agents.store import open_store
from threads.host.runs import Runner, RunTask
from threads.log import BranchId, ThreadId
from threads.reduce.wakes import pending_wakes
from threads.result import Ok
from threads.store import LOCAL_TENANT, StoreError
from threads.thread.handle import Thread

type OpenRun = tuple[str, ThreadId, BranchId]
"""(tenant, thread, branch) of an API run a crash may have left open."""

REOPEN_S = 1.0
"""How often a host looks again at an API run it could not resume yet, and a store error's
first wait."""

STORE_BACKOFF_MAX_S = 60.0
"""The longest wait between looks while a store error lasts."""

_log = logging.getLogger(__name__)


@dataclass(slots=True)
class _Streak:
    """A run's store errors in a row: the wait before the next look, and when that is."""

    wait: float
    at: float
    counted: RunTask
    """The latest run this streak has seen: its failure is counted once, and only a later run
    ending well ends the streak."""


class Reopening:
    """One host start's open API runs, and any run of this host that meets a store outage."""

    def __init__(self, runner: Runner) -> None:
        self._runner = runner
        self._open: dict[OpenRun, RunTask] = {}
        self._streaks: dict[OpenRun, _Streak] = {}

    async def first(self, rows: Sequence[OpenRun]) -> list[RunTask]:
        """The start's pass: each open, unparked run resumed. Returns the runs it started."""
        for row in rows:
            run = await self._reopen(row)
            if run is not None:
                self._open[row] = run
        return list(self._open.values())

    def watch(self, thread: Thread, run: RunTask) -> None:
        """A run of this host that met a store outage: looked at again, with backoff."""
        tenant = self._runner.tenant_of(thread.store)
        if tenant is not None:
            self._open.setdefault((tenant, thread.id, thread.branch), run)

    async def run(self) -> None:
        """Looks again each second at every run not yet closed, parked or given up on, and at
        every branch with a pending wake, until the host stops."""
        while True:
            await asyncio.sleep(REOPEN_S)
            await self._wakes()
            for row, run in tuple(self._open.items()):
                again = await self._look(row, run)
                if again is None:
                    del self._open[row]
                    self._streaks.pop(row, None)
                else:
                    self._open[row] = again

    async def _wakes(self) -> None:
        """Every branch with a pending wake not looked at yet is run on. A store error is said
        and looked at again on the next pass."""
        try:
            sq = await open_store(self._runner.store(LOCAL_TENANT))
            for row in await sq.tables.wake_branches():
                if row not in self._open:
                    run = await self._reopen(row)
                    if run is not None:
                        self._open[row] = run
        except StoreError as error:
            _log.warning("threads host: pending wakes not read (%s)", error)

    async def _look(self, row: OpenRun, run: RunTask) -> RunTask | None:
        """The run carrying the row after one more look, or None to stop looking."""
        if not run.done():
            return run
        error = None if run.cancelled() else run.exception()
        streak = self._streaks.get(row)
        fresh = streak is None or streak.counted is not run
        if isinstance(error, StoreError):
            if fresh:
                self._fail(row, error, run)
        elif _gave_up(row, run):
            return None
        elif fresh:
            # A run since the streak began ended without a store error: the streak is over.
            self._streaks.pop(row, None)
        streak = self._streaks.get(row)
        if streak is not None and asyncio.get_running_loop().time() < streak.at:
            return run
        try:
            return await self._reopen(row)
        except StoreError as failed:
            self._fail(row, failed, run)
            return run
        except Exception:
            # A bug, not a passing fault: said with its traceback, once.
            _log.exception("threads host: API run on %s not retried", row[2])
            return None

    async def _reopen(self, row: OpenRun) -> RunTask | None:
        """An open, unparked turn runs on from the log. What it left in doubt is settled by the
        loop's recovery, which never dispatches a begun effect again. Returns the run that
        carries it (the one already in flight here, if any); None once closed or parked."""
        tenant, thread, branch = row
        live = self._runner.live(branch)
        if live is not None:
            return live
        store = self._runner.store(tenant)
        read = await (await open_store(store)).read(branch, 0)
        if not isinstance(read, Ok):
            code, message = read.error.code, read.error.message
            _log.warning(
                "threads host: API run on %s not recovered (%s: %s)", branch, code, message
            )
            return None
        fold = read.value.fold
        waking = bool(pending_wakes(fold.events, branch))
        if (not fold.in_turn and not waking) or fold.parked:
            return None
        return await self._runner.resume(store, thread, branch)

    def _fail(self, row: OpenRun, error: StoreError, counted: RunTask) -> None:
        """One more store error in the row's streak: the next look waits twice as long."""
        streak = self._streaks.get(row)
        wait = REOPEN_S if streak is None else min(streak.wait * 2, STORE_BACKOFF_MAX_S)
        if streak is None:
            _log.warning(
                "threads host: API run on %s hit a store error; looking again with backoff (%s)",
                row[2],
                error,
            )
        at = asyncio.get_running_loop().time() + wait
        self._streaks[row] = _Streak(wait, at, counted)


def _gave_up(row: OpenRun, run: RunTask) -> bool:
    """The last run ended in a way another look won't change: anything but a lost lease or a
    store error. Said once, with the reason; the turn stays open in the log."""
    if not run.done() or run.cancelled():
        return False
    error = run.exception()
    if isinstance(error, StoreError):
        return False
    result = None if error is not None else run.result()
    if error is not None:
        why = f"{type(error).__name__}: {error}"
    elif isinstance(result, Failed) and result.error.code != "branch_busy":
        why = f"{result.error.code}: {result.error.message}"
    else:
        return False
    _log.warning("threads host: API run on %s not retried (%s)", row[2], why)
    return True
