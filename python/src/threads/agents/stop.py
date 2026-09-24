"""An early end stops the run's children (spec/schema/README.md, "Run completion"): a run that
ended failed, budget_exhausted or handed_off gives each background child it waits on (its own,
and an earlier run's it relaunched) a durable tree cancel, waits the step each needs to stop, and
records their ends with no wake. A cancel of the lead during that wait ends it."""

import asyncio
from collections.abc import Sequence
from typing import Final

from threads.agents.background import Background
from threads.agents.scope import Scope
from threads.log import AgentSpawnedEvent, CancelRequestedEvent, Event, UserInputEvent
from threads.loop.drive import drive
from threads.loop.runtime import Halt, Runtime
from threads.reduce.run_end import RunEnd, RunStatus, ended_otherwise, run_end
from threads.thread.tree import bar_child

REASON: Final = "parent run ended"
RETRY_S: Final = 0.05
"""How often a child not barred yet is tried again: its thread may not exist at the first try."""


def _latest(events: Sequence[Event]) -> RunEnd | None:
    request = next((e for e in reversed(events) if isinstance(e, UserInputEvent)), None)
    return None if request is None else run_end(events, request.event_id)


def run_status(events: Sequence[Event]) -> RunStatus:
    """The status of the latest request's run."""
    end = _latest(events)
    return "running" if end is None else end.status


def _waited_on(rt: Runtime, bg: Background) -> list[AgentSpawnedEvent]:
    """The background children this run waits on and that have not reported: its own, and any
    earlier run's it relaunched."""
    request = next((e for e in reversed(rt.events) if isinstance(e, UserInputEvent)), None)
    fold = rt.fold

    def waited(e: AgentSpawnedEvent) -> bool:
        child = e.data.child_thread_id
        if fold.children.get(child) is not False:
            return False
        run = fold.team.spawns.get(e.data.call_id)
        root = None if run is None else run.root
        mine = request is not None and root is not None and root[1] == request.event_id
        return mine or bg.has(child)

    return [
        e
        for e in rt.events
        if isinstance(e, AgentSpawnedEvent) and e.data.mode == "background" and waited(e)
    ]


def _cancelled_after(events: Sequence[Event], seq: int) -> bool:
    """A thread or tree cancel of the lead after `seq`."""
    return any(
        isinstance(e, CancelRequestedEvent) and e.seq > seq and e.data.scope != "turn"
        for e in events
    )


async def stop_children[D](scope: Scope[D], rt: Runtime, bg: Background, halt: Halt) -> Halt:
    # A cancel of the lead after the run's deciding event ends the wait.
    end = _latest(rt.events)
    decided = rt.fold.seq if end is None or end.at is None else rt.events[end.at].seq
    barred: set[str] = set()
    left = _waited_on(rt, bg)
    while left and ended_otherwise(run_status(rt.events)):
        # Taken before the checks: a cancel that lands after them still wakes the wait.
        moved = rt.writer.moved()
        if _cancelled_after(rt.events, decided):
            moved.close()
            return halt
        for spawned in left:
            child = spawned.data.child_thread_id
            if child not in barred and await bar_child(scope.store, child, scope.principal, REASON):
                barred.add(child)
        running = [s for s in left if s.data.child_thread_id in bg.running]
        if not running and not bg.ended:
            moved.close()
            return halt
        if bg.ended:
            moved.close()
        else:
            unbarred = any(s.data.child_thread_id not in barred for s in left)
            # ponytail: re-sends every 50 ms until each child has its barrier; a start
            # notification would do it without a timer.
            timer = (asyncio.sleep(RETRY_S),) if unbarred else ()
            await bg.next_end(moved, *timer)
        halt = await drive(rt)
        left = _waited_on(rt, bg)
    return halt
