"""The parent side of the crash drills: spawns worker.py processes on one store, stops or kills
them at scripted points, and reads the outcome back from the log and the fake provider's
files."""

import asyncio
import contextlib
import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

from jobs.stores import drill_open, query
from jobs.worker import TEAM, read, rows

from threads.log import EventId
from threads.reduce import Fold
from threads.result import Ok

WORKER: Final = Path(__file__).with_name("worker.py")
WAIT_S: Final = 20.0
TESTS: Final = str(Path(__file__).parent.parent)
_SPAWNED: list[subprocess.Popen[str]] = []


def spawn(role: str, where: Path, script: Path = WORKER, **env: str) -> subprocess.Popen[str]:
    # The worker is this repo's own test script, run by this interpreter.
    # Each worker's stderr goes to its own file, so a failed drill shows what every process did.
    with (where / f"worker-{len(_SPAWNED)}.log").open("w") as errors:
        worker = subprocess.Popen(  # noqa: S603
            [sys.executable, str(script), role, str(where)],
            env={**os.environ, "PYTHONPATH": TESTS, **env},
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
            start_new_session=True,
        )
    _SPAWNED.append(worker)
    return worker


def reap() -> None:
    """After every drill, passed or failed: no worker, nor anything it started, outlives it."""
    groups: list[int] = []
    while _SPAWNED:
        worker = _SPAWNED.pop()
        with contextlib.suppress(ProcessLookupError):
            os.killpg(worker.pid, signal.SIGKILL)
        worker.communicate()
        groups.append(worker.pid)
    alive = live_groups(groups)
    assert not alive, f"worker process groups still running: {alive}"


def live_groups(groups: list[int]) -> list[int]:
    """The process groups that still have a member. Each worker leads its own group
    (`start_new_session`), so its pid is the group id. Signal 0 checks without pgrep, which
    slim Linux images lack."""
    alive: list[int] = []
    for group in groups:
        try:
            os.killpg(group, 0)
        except ProcessLookupError:
            continue
        alive.append(group)
    return alive


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
    query(where, "UPDATE leases SET expires_at = 0")


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
    opened = await drill_open(where, TEAM)
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
