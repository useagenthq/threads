"""Job `api-run-kill-restart`: a host running a run started through the run API is killed with
SIGKILL, then a host that is given no input starts on the same store. The run's open turn goes on
from the log; an effect that began is never run again; two hosts starting together resume it
once."""

import asyncio
import subprocess
import time
from pathlib import Path
from typing import Final

import pytest
from jobs.api_worker import TENANT, read
from jobs.drill import expire_leases, finish, kill, one_writer_at_a_time, spawn, wait_at
from jobs.stores import drill_open
from jobs.worker import rows

from threads.log import (
    EffectBeginEvent,
    Event,
    ModelResponseEvent,
    ParkedEvent,
    TurnCompletedEvent,
    UserInputEvent,
)
from threads.reduce import Fold
from threads.result import Ok

pytestmark = pytest.mark.jobs

API_WORKER: Final = Path(__file__).with_name("api_worker.py")


def api(where: Path, **env: str) -> subprocess.Popen[str]:
    # The worker imports the drill kit as `jobs.worker`, as the tests do.
    return spawn("api", where, API_WORKER, PYTHONPATH=str(API_WORKER.parent.parent), **env)


def log(where: Path) -> Fold:
    fold = asyncio.run(_read(where))
    assert fold is not None
    return fold


async def _read(where: Path) -> Fold | None:
    # Closed after each read: the drill's processes own the store.
    opened = await drill_open(where, TENANT)
    assert isinstance(opened, Ok)
    try:
        return await read(opened.value)
    finally:
        await opened.value.close()


def count(fold: Fold, kind: type[Event]) -> int:
    return sum(isinstance(e, kind) for e in fold.events)


def killed(where: Path, point: str, **env: str) -> None:
    """A started run whose host was killed at `point`, its lease still live."""
    first = api(where, DRILL_START="1", DRILL_STOP_AT=point, **env)
    wait_at(first, point)
    kill(first)


def crashed(where: Path, point: str, **env: str) -> None:
    """The same, once its lease has run out."""
    killed(where, point, **env)
    expire_leases(where)


def test_killed_at_the_model_request_a_host_given_no_input_completes_it(tmp_path: Path) -> None:
    crashed(tmp_path, "model_request")
    before = [e.event_id for e in log(tmp_path).events]

    finish(api(tmp_path))
    fold = log(tmp_path)
    assert [e.event_id for e in fold.events][: len(before)] == before
    assert count(fold, UserInputEvent) == 1
    assert count(fold, ModelResponseEvent) == 1
    assert isinstance(fold.events[-1], TurnCompletedEvent)
    one_writer_at_a_time(fold)

    # A second restart finds nothing to do.
    settled = [e.event_id for e in fold.events]
    finish(api(tmp_path))
    assert [e.event_id for e in log(tmp_path).events] == settled


def test_restarted_at_once_the_turn_completes_when_the_dead_lease_runs_out(
    tmp_path: Path,
) -> None:
    killed(tmp_path, "model_request")
    restarted = api(tmp_path)
    # Its first pass finds the branch leased by the dead host.
    time.sleep(2)
    assert count(log(tmp_path), TurnCompletedEvent) == 0
    expire_leases(tmp_path)
    finish(restarted)
    fold = log(tmp_path)
    assert count(fold, ModelResponseEvent) == 1
    assert isinstance(fold.events[-1], TurnCompletedEvent)
    one_writer_at_a_time(fold)


def test_killed_inside_an_effect_the_restart_parks_it_and_never_runs_it_again(
    tmp_path: Path,
) -> None:
    crashed(tmp_path, "effect_begin", DRILL_CHARGE="1")
    assert len(rows(tmp_path / "charges.jsonl")) == 1

    finish(api(tmp_path, DRILL_CHARGE="1"))
    fold = log(tmp_path)
    assert len(rows(tmp_path / "charges.jsonl")) == 1
    assert count(fold, EffectBeginEvent) == 1
    assert count(fold, ParkedEvent) == 1
    assert count(fold, TurnCompletedEvent) == 0
    one_writer_at_a_time(fold)


def test_two_hosts_starting_together_resume_it_once(tmp_path: Path) -> None:
    crashed(tmp_path, "model_request")
    before = len(rows(tmp_path / "model.jsonl"))
    a = api(tmp_path, DRILL_GO="1")
    b = api(tmp_path, DRILL_GO="1")
    (tmp_path / "go").touch()
    finish(a)
    finish(b)
    fold = log(tmp_path)
    assert len(rows(tmp_path / "model.jsonl")) - before == 1
    assert count(fold, ModelResponseEvent) == 1
    assert count(fold, TurnCompletedEvent) == 1
    one_writer_at_a_time(fold)
