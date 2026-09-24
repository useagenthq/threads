"""Loop-bound adapter resources, with one owner per event loop.

An aiohttp session, an httpx pool or a gRPC channel belongs to the event loop it was made on,
but an agent is reused across runs and runs can be on different loops (`run_sync`, repeated
`asyncio.run`, several threads). So an adapter keeps its clients in a `LoopResources`, one
bundle per loop, never on itself.

The owner is whoever holds the loop (`holding()`): a run for its whole life, a host from
`ready()` to `stop()`, and CLI `gc` and `Thread.fork` for their own calls. When the last hold
on a loop ends, in one synchronous step every bundle made on it is retired (removed from its
adapter, so a new hold builds fresh ones and never touches one being closed); then the loop's
background tasks are drained, so none is left using a client; then the bundles are closed,
newest first, each attempted even if another fails. Nothing relies on garbage collection.
"""

import asyncio
import logging
import threading
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import Final

from threads.redaction.text import redact_secrets

GRACE_S: Final = 0.25
"""How long a release waits for the loop's background tasks before cancelling them."""
KEPT: Final = 32
"""Failure records a loop keeps for its release report. Every failure is logged when it
happens, so a record dropped from this ring is still in the logs."""
_MESSAGE_MAX: Final = 1024

_log = logging.getLogger("threads")


@dataclass(frozen=True, slots=True)
class _Closer:
    name: str
    retire: Callable[[], object]
    close: Callable[[], Awaitable[None]]


@dataclass(slots=True)
class _Entry:
    """One loop's holds, the bundles made on it, and its background tasks."""

    loop: asyncio.AbstractEventLoop
    holds: int = 0
    closers: list[_Closer] = field(default_factory=list[_Closer])
    tasks: set[asyncio.Task[object]] = field(default_factory=set[asyncio.Task[object]])
    """Background tasks whose outcome is not collected yet."""
    failures: deque[str] = field(default_factory=lambda: deque[str](maxlen=KEPT))
    dropped: int = 0


_lock = threading.Lock()
"""Guards the registry and every adapter's bundle map across threads; no I/O under it."""
_entries: dict[int, _Entry] = {}
"""Keyed by id(loop), with the loop kept in the entry: a dead loop's reused id is a miss."""


def _held(loop: asyncio.AbstractEventLoop) -> _Entry:
    entry = _entries.get(id(loop))
    if entry is None or entry.loop is not loop:
        raise AssertionError("an adapter used its connections with no hold on this event loop")
    return entry


class LoopResources[T]:
    """One adapter's loop-bound bundle, one per event loop, closed by the loop's owner."""

    def __init__(self, name: str, close: Callable[[T], Awaitable[None]]) -> None:
        self._name: Final = name
        self._close: Final = close
        self._bundles: dict[int, tuple[asyncio.AbstractEventLoop, T]] = {}

    def get(self, make: Callable[[], T]) -> T:
        """The running loop's bundle, made by `make` on first use there. `make` does no I/O."""
        loop = asyncio.get_running_loop()
        with _lock:
            entry = _held(loop)
            found = self._bundles.get(id(loop))
            if found is not None and found[0] is loop:
                return found[1]
            bundle = make()
            self._bundles[id(loop)] = (loop, bundle)
            retire = partial(self._bundles.pop, id(loop), None)
            entry.closers.append(_Closer(self._name, retire, partial(self._close, bundle)))
            return bundle


def spawn_owned[T](work: Coroutine[object, object, T], name: str) -> "asyncio.Task[T]":
    """Runs `work` in the background on the running loop, owned by it: a release waits for it
    (then cancels it) before closing any client, and its failure is logged, never lost."""
    loop = asyncio.get_running_loop()
    with _lock:
        try:
            entry = _held(loop)
        except AssertionError:
            work.close()
            raise
    task = loop.create_task(work, name=name)
    entry.tasks.add(task)
    task.add_done_callback(partial(_collect, entry))
    return task


def _collect(entry: _Entry, task: "asyncio.Task[object]") -> None:
    """Records a finished task's outcome exactly once: its done callback and a release both
    call this, in either order."""
    if task not in entry.tasks or not task.done():
        return
    entry.tasks.discard(task)
    if task.cancelled() or (error := task.exception()) is None:
        return
    record = _record(task.get_name(), error)
    _log.warning("threads: background task failed: %s", record)
    if len(entry.failures) == KEPT:
        entry.dropped += 1
    entry.failures.append(record)


def _record(name: str, error: BaseException) -> str:
    message = redact_secrets(str(error))[:_MESSAGE_MAX]
    return f"{name}: {type(error).__name__}: {message}"


def _hold() -> _Entry:
    loop = asyncio.get_running_loop()
    with _lock:
        entry = _entries.get(id(loop))
        if entry is None or entry.loop is not loop:
            entry = _entries[id(loop)] = _Entry(loop)
        entry.holds += 1
        return entry


async def _release(entry: _Entry) -> list[str]:
    """Drops one hold. The last one retires, drains and closes; returns what failed. A
    cancellation that arrives meanwhile (a second Ctrl-C, a host stop cut short) doesn't cut
    it short: the bundles are already retired, so nothing else would close them. Each step
    runs to its end and the cancellation is raised after the last."""
    with _lock:
        entry.holds -= 1
        if entry.holds > 0:
            return []
        if _entries.get(id(entry.loop)) is entry:
            del _entries[id(entry.loop)]
        for closer in entry.closers:
            closer.retire()
    _, cancelled = await _to_the_end(partial(_drain, entry))
    report = list(entry.failures)
    if entry.dropped:
        report.append(f"{entry.dropped} more failures were logged earlier")
    for closer in reversed(entry.closers):
        failed, stopped = await _to_the_end(closer.close)
        cancelled = cancelled or stopped
        if failed is not None:
            report.append(_record(f"closing {closer.name}", failed))
    if cancelled is not None:
        for line in report:
            _log.warning("threads: while closing connections: %s", line)
        raise cancelled
    return report


async def _drain(entry: _Entry) -> None:
    tasks = set(entry.tasks)
    await drain(tasks, GRACE_S)
    # A task can be done with its done callback still queued: collect it now, so the report
    # has it; the callback then finds it collected.
    for task in tasks:
        _collect(entry, task)


async def _attempt(step: Callable[[], Awaitable[object]]) -> Exception | None:
    """Runs `step`; what it raised comes back as a value."""
    try:
        await step()
    except Exception as error:
        return error
    return None


async def _to_the_end(
    step: Callable[[], Awaitable[object]],
) -> tuple[Exception | None, asyncio.CancelledError | None]:
    """Runs `step` to its end even if this task is cancelled meanwhile; returns what it raised
    and the cancellation that arrived, for the caller to raise when it is done."""
    running = asyncio.ensure_future(_attempt(step))
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            return await asyncio.shield(running), cancelled
        except asyncio.CancelledError as error:
            if running.cancelled():  # the step itself was cancelled, not this task
                return None, error
            cancelled = error


async def drain[T](tasks: Iterable["asyncio.Task[T]"], grace_s: float) -> None:
    """Waits up to `grace_s` for `tasks`, then cancels those still running and waits for them
    to end."""
    if running := [task for task in tasks if not task.done()]:
        _, stuck = await asyncio.wait(running, timeout=grace_s)
        for task in stuck:
            task.cancel()
        await asyncio.gather(*stuck, return_exceptions=True)


async def close_all(steps: Iterable[Callable[[], Awaitable[object]]]) -> None:
    """Runs every close step, in order, even when one fails; then raises the first failure,
    with the others as notes."""
    failed = [error for step in steps if (error := await _attempt(step)) is not None]
    if failed:
        for other in failed[1:]:
            failed[0].add_note(f"also: {_record('close', other)}")
        raise failed[0]


@asynccontextmanager
async def holding() -> AsyncGenerator[None]:
    """Holds the running loop's adapter resources for the body. When the last hold ends they
    are closed; a failure to close never replaces the body's own error (it becomes a note on
    it) and never turns a finished body into an error (it is logged)."""
    entry = _hold()
    try:
        yield
    except BaseException as error:
        for line in await _release(entry):
            error.add_note(f"while closing connections: {line}")
        raise
    for line in await _release(entry):
        _log.warning("threads: while closing connections: %s", line)
