"""`host(telemetry=...)`: the exporter syncs in its own loop beside the scheduler, never inside
it, so a slow or dead collector holds up no run, schedule or reply. A failing collector is
retried with back-off (1 s doubling to 60 s) and logged once per streak; a branch the exporter
can't read is logged once per head it fails at (its back-off streak)."""

import asyncio
import logging
import time
from dataclasses import dataclass

from threads.result import Err
from threads.telemetry import Exporter, SkippedBranch


@dataclass(frozen=True, slots=True)
class Timing:
    """How often the host syncs, the longest back-off, and the bound on the last sync."""

    every_s: float = 1.0
    max_wait_s: float = 60.0
    last_sync_s: float = 5.0


_log = logging.getLogger(__name__)


class Telemetry:
    def __init__(self, exporter: Exporter, timing: Timing | None = None) -> None:
        self._exporter = exporter
        self._timing = timing or Timing()
        self._logged: set[tuple[str, int]] = set()
        self._wait = 0.0
        self._next_at = 0.0
        self._lock = asyncio.Lock()

    async def run(self) -> None:
        """Syncs every second (less often while the collector fails) until cancelled."""
        while True:
            await asyncio.sleep(self._timing.every_s)
            if time.monotonic() >= self._next_at:
                await self._sync()

    async def last(self) -> None:
        """stop(): one last sync, bounded by 5 s; a timeout gives up quietly."""
        try:
            await asyncio.wait_for(self._sync(), self._timing.last_sync_s)
        except TimeoutError:
            _log.warning("threads host: the last telemetry sync gave up after its bound")

    async def _sync(self) -> None:
        async with self._lock:
            try:
                sent = await self._exporter.sync()
            except Exception:
                _log.exception("threads host: telemetry failed")
                return
        if isinstance(sent, Err):
            if self._wait == 0:
                _log.warning(
                    "threads host: telemetry %s: %s; retrying with backoff",
                    sent.error.code,
                    sent.error.message,
                )
            t = self._timing
            self._wait = min(max(self._wait * 2, t.every_s), t.max_wait_s)
            self._next_at = time.monotonic() + self._wait
            return
        self._wait, self._next_at = 0.0, 0.0
        for skipped in sent.value.skipped:
            self._skipped(skipped)

    def _skipped(self, s: SkippedBranch) -> None:
        key = (s.branch_id, s.head_seq)
        if key in self._logged:
            return
        self._logged.add(key)
        _log.warning(
            "threads host: telemetry skipped branch %s of thread %s (%s); retrying with backoff",
            s.branch_id,
            s.thread_id,
            s.code,
        )
