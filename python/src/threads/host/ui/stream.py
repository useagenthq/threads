"""One UI stream of one run, from the log (spec/schema/ui/README.md): a UiSession fed the
branch's log each time it may have changed and the live deltas this process's runs publish,
until the run has an outcome. It reads the log and starts nothing."""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from threads.agents.store import now_ms, open_store
from threads.host.outcome import logged, run_start
from threads.host.runs import Runner
from threads.host.stream import unlogged
from threads.host.ui.closing import ending
from threads.host.ui.frame import Frame
from threads.host.ui.listener import LiveListener
from threads.host.ui.session import SessionPlan, UiSession
from threads.reduce.fold import loop_parked
from threads.result import Ok
from threads.store import VerifiedLog
from threads.thread.handle import Thread


@dataclass(frozen=True, slots=True)
class End:
    """`done` (the AI SDK stream then sends [DONE]) or `broken` (no [DONE])."""

    how: Literal["done", "broken"]


type Out = Frame | End


async def ui_frames(
    runner: Runner, thread: Thread, plan: SessionPlan, listener: LiveListener
) -> AsyncGenerator[Out]:
    """The stream. `listener` was registered with the live hub before the run started or its
    head was read; without live text (the cursor route) it only polls."""
    try:
        first = await _read(thread)
        if first is None:
            return
        session = UiSession(plan, first.fold.events, listener.missed)
        for f in session.opening(first.fold.events):
            yield f
        while True:
            for f in session.deltas(listener.take()):
                yield f
            read = await _read(thread)
            if read is None:
                return
            step = session.read(read.fold.events)
            for f in step.frames:
                yield f
            if step.broken is not None:
                yield End("broken")
                return
            outcome = _outcome(runner, thread, plan, read)
            if outcome is not None:
                for f in session.close(ending(outcome)):
                    yield f
                yield End("done")
                return
            await listener.next()
    finally:
        listener.stop()


def _outcome(
    runner: Runner, thread: Thread, plan: SessionPlan, read: VerifiedLog
) -> JsonValue | None:
    events = read.fold.events
    start = run_start(events, plan.run_id)
    if start is None:
        return None
    return logged(events, start, loop_parked(read.fold), thread) or unlogged(runner, thread.branch)


async def _read(thread: Thread) -> VerifiedLog | None:
    read = await (await open_store(thread.store)).read(thread.branch, now_ms())
    return read.value if isinstance(read, Ok) else None
