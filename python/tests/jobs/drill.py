"""The parent side of the crash drills: spawns worker.py processes on one store, stops or kills
them at scripted points, and reads the outcome back from the log and the fake provider's
files."""

import asyncio
import contextlib
import os
import select
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

from jobs.worker import TEAM, read, rows

from threads.log import EventId
from threads.reduce import Fold
from threads.result import Ok
from threads.store import SqliteStore

WORKER: Final = Path(__file__).with_name("worker.py")
WAIT_S: Final = 20.0
_SPAWNED: list[subprocess.Popen[str]] = []


def spawn(role: str, where: Path, **env: str) -> subprocess.Popen[str]:
    # The worker is this repo's own test script, run by this interpreter.
    worker = subprocess.Popen(  # noqa: S603
        [sys.executable, str(WORKER), role, str(where)],
        env={**os.environ, **env},
        stdout=subprocess.PIPE,
        text=True,
    )
    _SPAWNED.append(worker)
    return worker


def reap() -> None:
    """After every drill, passed or failed: no worker outlives it."""
    while _SPAWNED:
        worker = _SPAWNED.pop()
        if worker.poll() is None:
            worker.kill()
        worker.communicate()


def wait_at(worker: subprocess.Popen[str], point: str) -> None:
    """Until the worker reports it is blocked at `point`."""
    assert worker.stdout is not None
    ready, _, _ = select.select([worker.stdout], [], [], WAIT_S)
    assert ready, f"the worker never reached {point}"
    line = worker.stdout.readline()
    assert line.strip() == f"at {point}", f"the worker ended before {point}: {line!r}"


def kill(worker: subprocess.Popen[str]) -> None:
    worker.send_signal(signal.SIGKILL)
    worker.communicate(timeout=WAIT_S)
    assert worker.returncode == -signal.SIGKILL


def expire_leases(where: Path) -> None:
    """A killed holder's lease runs out after its TTL (30 s); the drill moves its expiry to now
    instead of waiting, which is all the wait would change."""
    with contextlib.closing(sqlite3.connect(where / "threads.db")) as db, db:
        db.execute("UPDATE leases SET expires_at = 0")


def finish(worker: subprocess.Popen[str]) -> None:
    worker.communicate(timeout=WAIT_S)
    assert worker.returncode == 0


def release(where: Path) -> None:
    (where / "release").touch()


def log(where: Path) -> Fold:
    fold = asyncio.run(_read(where))
    assert fold is not None
    return fold


def acked(where: Path) -> list[EventId]:
    """Every event the conversation's log holds; none before its first message is consumed."""
    fold = asyncio.run(_read(where))
    return [] if fold is None else [e.event_id for e in fold.events]


async def _read(where: Path) -> Fold | None:
    # Closed after each read: the drill's processes own the store.
    opened = await SqliteStore.open(where / "threads.db", tenant_id=TEAM)
    assert isinstance(opened, Ok)
    try:
        return await read(opened.value)
    finally:
        await opened.value.close()


def sends(where: Path) -> list[str]:
    return [str(r["key"]) for r in rows(where / "sends.jsonl")]


def pids(where: Path, name: str) -> set[int]:
    return {int(str(r["pid"])) for r in rows(where / name)}


def one_writer_at_a_time(fold: Fold) -> None:
    """No interleaved appends: seqs are contiguous and each epoch's events are one run of the
    log, so a writer never appended after a newer one took the branch."""
    assert [e.seq for e in fold.events] == list(range(1, len(fold.events) + 1))
    epochs = [e.epoch for e in fold.events]
    assert epochs == sorted(epochs)


def until(probe: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + WAIT_S
    while not probe():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.02)
