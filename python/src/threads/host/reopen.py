"""API runs a crash left open, resumed by a starting host and looked at again until each closes or
parks (spec/schema/README.md, "API run recovery").

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
from threads.host.runs import Runner, RunTask
from threads.log import BranchId, ThreadId
from threads.store import StoreError

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
    """One host start's open API runs."""

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

    async def run(self) -> None:
        """Looks again each second until every run closed or parked, or was given up on."""
        while self._open:
            await asyncio.sleep(REOPEN_S)
            for row, run in tuple(self._open.items()):
                again = await self._look(row, run)
                if again is None:
                    del self._open[row]
                    self._streaks.pop(row, None)
                else:
                    self._open[row] = again

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
        tenant, thread, branch = row
        return await self._runner.reopen(self._runner.store(tenant), thread, branch)

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
