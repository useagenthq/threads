"""`Exporter.sync()` (spec/otel/README.md, "The cursor, sync and losses"): every changed
branch's newly closed spans, posted in batches; a branch's cursor moves only after the collector
accepted its spans, so a crash re-sends with the same ids and never drops one."""

import asyncio
import time
from collections import Counter
from dataclasses import dataclass

from pydantic.experimental.missing_sentinel import MISSING

from threads import VERSION
from threads.agents.config import ConfigError
from threads.agents.store import Store, now_ms, open_store
from threads.log import BranchId, ThreadStartedEvent
from threads.otel.backoff import Backoff
from threads.otel.env import Config
from threads.otel.losses import loss_spans
from threads.otel.otlp import MAX_BATCH, batches, body
from threads.otel.send import post
from threads.otel.span import Span
from threads.otel.spans import Branch, spans
from threads.result import Err, Ok
from threads.store._feed import ChangedBranch, Checkpoint, Feed
from threads.store.verify import VerifiedLog
from threads.telemetry import SkippedBranch, SyncError, SyncReport, bound_store


@dataclass(frozen=True, slots=True)
class _Ready:
    branch: ChangedBranch
    head: int
    spans: tuple[Span, ...]


class OtelExporter:
    """spec/api.json `Exporter`, made by `otel()`."""

    def __init__(
        self, config: Config, store: Store | None, observer: str, *, content: bool
    ) -> None:
        self._config = config
        self._store = store
        self._observer = observer
        self._content = content
        self._backoff = Backoff()
        self._lock = asyncio.Lock()

    async def sync(self) -> Ok[SyncReport] | Err[SyncError]:
        """One sync at a time: a second call waits for the first."""
        async with self._lock:
            return await self._sync()

    async def _sync(self) -> Ok[SyncReport] | Err[SyncError]:
        store = self._store if self._store is not None else bound_store(self)
        if store is None:
            raise ConfigError(
                "invalid_config",
                "otel(): no store to export: pass otel(store=...), or pass the exporter to"
                " host(telemetry=...)",
            )
        feed = (await open_store(store)).feed(self._observer, now_ms)
        await feed.register()
        skipped: list[SkippedBranch] = []
        ready = await self._read(feed, await feed.changed(), skipped)
        await feed.checkpoint(
            [Checkpoint(r.branch.branch_id, r.head) for r in ready if not r.spans]
        )
        sent = await self._send(feed, ready)
        if isinstance(sent, Err):
            return sent
        lost = await self._losses(feed)
        if isinstance(lost, Err):
            return lost
        return Ok(SyncReport(sent.value, lost.value, tuple(skipped)))

    async def _read(
        self, feed: Feed, changed: list[ChangedBranch], skipped: list[SkippedBranch]
    ) -> list[_Ready]:
        """Each changed branch's chain and newly closed spans; one that doesn't read is
        skipped."""
        chains: dict[str, VerifiedLog | None] = {}
        """Every chain read this sync, for a child's parents."""
        ready: list[_Ready] = []
        now = time.monotonic_ns() // 1_000_000
        for branch in changed:
            if self._backoff.waiting(branch.branch_id, branch.head_seq, now):
                continue
            read = await feed.chain(branch.branch_id)
            if isinstance(read, Err):
                self._backoff.failed(branch.branch_id, branch.head_seq, now)
                skipped.append(
                    SkippedBranch(
                        branch.branch_id, branch.thread_id, read.error.code, branch.head_seq
                    )
                )
                continue
            self._backoff.cleared(branch.branch_id)
            chains[branch.branch_id] = read.value
            ready.append(await self._fresh(feed, branch, read.value, chains))
            # Deriving a long chain is CPU work on the loop: let the host's other work run
            # between branches.
            await asyncio.sleep(0)
        return ready

    async def _fresh(
        self,
        feed: Feed,
        branch: ChangedBranch,
        log: VerifiedLog,
        chains: dict[str, VerifiedLog | None],
    ) -> _Ready:
        """The branch's spans that closed past its cursor, parents read on demand."""
        wanted = _parent_of(log)
        while wanted is not None and wanted not in chains:
            read = await feed.chain(wanted)
            chains[wanted] = read.value if isinstance(read, Ok) else None
            wanted = None if isinstance(read, Err) else _parent_of(read.value)
        head = log.head.seq
        found = spans(Branch(branch.tenant_id, branch.branch_id, log, self._content), chains.get)
        fresh = tuple(s for s in found if branch.cursor < s.close_seq <= head)
        return _Ready(branch, head, fresh)

    async def _send(self, feed: Feed, ready: list[_Ready]) -> Ok[int] | Err[SyncError]:
        """Posts the spans in batches; after each 2xx, each branch in it moves as far as it is
        sent."""
        by_id: dict[str, _Ready] = {r.branch.branch_id: r for r in ready}
        left: Counter[str] = Counter({b: len(r.spans) for b, r in by_id.items()})
        sent = 0
        for batch in batches([s for r in ready for s in r.spans]):
            posted = await post(self._config, body(batch, self._config.resource, VERSION))
            if isinstance(posted, Err):
                return posted
            sent += len(batch)
            through: dict[str, int] = {}
            for s in batch:
                left[s.branch_id] -= 1
                through[s.branch_id] = max(through.get(s.branch_id, 0), s.close_seq)
            await feed.checkpoint(
                [
                    Checkpoint(by_id[b].branch.branch_id, by_id[b].head if left[b] == 0 else seq)
                    for b, seq in through.items()
                ]
            )
        return Ok(sent)

    async def _losses(self, feed: Feed) -> Ok[int] | Err[SyncError]:
        """The unreported deletion losses, as possibly_lost spans after the branch batches."""
        rows = await feed.unreported_losses()
        lost = 0
        for at in range(0, len(rows), MAX_BATCH):
            chunk = rows[at : at + MAX_BATCH]
            spans_ = loss_spans(self._observer, chunk)
            posted = await post(self._config, body(spans_, self._config.resource, VERSION))
            if isinstance(posted, Err):
                return posted
            await feed.mark_reported(chunk)
            lost += sum(r.unchecked_events for r in chunk)
        return Ok(lost)


def _parent_of(log: VerifiedLog) -> BranchId | None:
    """The branch a child thread's first turn is parented through, if it is a child."""
    for segment in log.segments[:1]:
        for event, _ in segment.events[:1]:
            if isinstance(event, ThreadStartedEvent) and event.data.parent is not MISSING:
                return event.data.parent.branch_id
    return None
