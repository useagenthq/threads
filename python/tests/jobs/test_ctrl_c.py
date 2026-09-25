"""Ctrl-C and `run_sync`, as `asyncio.run` behaves: the first Ctrl-C cancels the run, which
cleans up (lease released, connections closed) and raises KeyboardInterrupt; a tool that
ignores cancellation can keep the process alive, and after it is killed the thread is usable
once its lease expires, with the tool's effect recorded once, never repeated."""

import asyncio
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from jobs.drill import TESTS, WAIT_S, expire_leases
from jobs.stores import drill_store
from loop_kit import BYPASS, text, use
from pydantic import BaseModel

from threads import Completed, RunContext, agent, scripted_model, tool
from threads.agents.store import Store, open_store

pytestmark = pytest.mark.jobs
RESIST = Path(__file__).with_name("resist_worker.py")


class Nothing(BaseModel):
    pass


def test_first_ctrl_c_is_cooperative(tmp_path: Path) -> None:
    started = threading.Event()
    calls: list[int] = []

    async def slow(_args: Nothing, _ctx: RunContext[None]) -> str:
        calls.append(1)
        if len(calls) == 1:
            started.set()
            await asyncio.sleep(60)  # cancellable
        return "done"

    waits = tool(name="slow", description="Waits.", input=Nothing, execute=slow, effect="read_only")
    replies = [text("ok"), use("slow", {}), text("after"), text("after")]
    bot = agent(model=scripted_model({"responses": replies}), tools=[waits], permissions=BYPASS)
    store = drill_store(tmp_path)
    first = bot.run_sync("hi", store=store, deps=None)

    def interrupt() -> None:
        assert started.wait(WAIT_S)
        os.kill(os.getpid(), signal.SIGINT)  # the Runner's handler cancels the run

    threading.Thread(target=interrupt).start()
    with pytest.raises(KeyboardInterrupt):
        bot.run_sync("wait", thread=first.thread, deps=None)
    # Cleaned up on the way out: the lease was handed back, so the thread runs at once.
    again = bot.run_sync("again", thread=first.thread, deps=None)
    assert isinstance(again, Completed), again
    asyncio.run(_close(store))


async def _close(store: Store) -> None:
    await (await open_store(store)).close()


def _spawn(*args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603 - this repo's own test script
        [sys.executable, str(RESIST), *args],
        env={**os.environ, "PYTHONPATH": TESTS},
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )


def _line(worker: subprocess.Popen[str]) -> str:
    assert worker.stdout is not None
    ready, _, _ = select.select([worker.stdout], [], [], WAIT_S)
    assert ready, "the worker printed nothing"
    return worker.stdout.readline().strip()


def _again(where: Path, thread: str) -> list[str]:
    worker = _spawn("again", str(where), thread)
    out, _ = worker.communicate(timeout=WAIT_S)
    return out.split()


def test_a_tool_that_ignores_cancellation(tmp_path: Path) -> None:
    worker = _spawn("first", str(tmp_path))
    try:
        at, _, thread = _line(worker).partition(" tool ")
        assert at == "at"
        for _ in range(2):
            worker.send_signal(signal.SIGINT)
            assert _line(worker) == "resisting"
        time.sleep(0.2)
        assert worker.poll() is None  # the second Ctrl-C can't end it either
    finally:
        worker.kill()
        worker.communicate(timeout=WAIT_S)
    assert _again(tmp_path, thread)[:1] == ["branch_busy"]  # its lease hasn't expired
    expire_leases(tmp_path)  # what waiting out the 30 s lease would change
    status, *counts = _again(tmp_path, thread)
    # The run parks on the effect it can't prove: recorded once, never repeated.
    assert status == "parked"
    assert counts == ["ran", "1", "begun", "1"]
