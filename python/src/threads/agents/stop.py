"""An early end stops the run's children (spec/schema/README.md, "Run completion"): a run that
ended failed, budget_exhausted or handed_off gives each of its background children that has not
reported a durable tree cancel, waits the step each needs to stop, and records their ends with no
wake, so no running child or pending_wakes row outlives run()."""

import asyncio
from collections.abc import Sequence
from typing import Final

from threads.agents.background import Background
from threads.agents.scope import Scope
from threads.log import AgentSpawnedEvent, Event, UserInputEvent
from threads.loop.drive import drive
from threads.loop.runtime import Halt, Runtime
from threads.reduce.run_end import RunStatus, ended_otherwise, run_end
from threads.thread.tree import bar_child

REASON: Final = "parent run ended"
RETRY_S: Final = 0.05
"""How often an unstarted child is barred again: its thread may not exist at the first try."""


def run_status(events: Sequence[Event]) -> RunStatus:
    """The status of the latest request's run."""
    request = next((e for e in reversed(events) if isinstance(e, UserInputEvent)), None)
    return "running" if request is None else run_end(events, request.event_id).status


def _unreported(rt: Runtime) -> list[AgentSpawnedEvent]:
    """This run's background children still running or not yet recorded."""
    request = next((e for e in reversed(rt.events) if isinstance(e, UserInputEvent)), None)
    if request is None:
        return []
    fold = rt.fold

    def mine(e: AgentSpawnedEvent) -> bool:
        run = fold.team.spawns.get(e.data.call_id)
        root = None if run is None else run.root
        running = not fold.children.get(e.data.child_thread_id)
        return running and root is not None and root[1] == request.event_id

    return [
        e
        for e in rt.events
        if isinstance(e, AgentSpawnedEvent) and e.data.mode == "background" and mine(e)
    ]


async def stop_children[D](scope: Scope[D], rt: Runtime, bg: Background, halt: Halt) -> Halt:
    left = _unreported(rt)
    while left and ended_otherwise(run_status(rt.events)):
        for spawned in left:
            await bar_child(scope.store, spawned.data.child_thread_id, scope.principal, REASON)
        running = [
            bg.running[s.data.child_thread_id] for s in left if s.data.child_thread_id in bg.running
        ]
        if not running and not bg.ended:
            return halt
        if not bg.ended:
            # ponytail: polls every 50 ms; a start notification would do it without a timer.
            await bg.next_end(rt.writer.moved(), asyncio.sleep(RETRY_S))
        halt = await drive(rt)
        left = _unreported(rt)
    return halt
