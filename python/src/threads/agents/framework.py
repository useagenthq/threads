"""The framework tools of one run, as the loop's `Framework`: spawn_agent, handoff and
the team tools each advance their call on the log; background children run beside the loop and
are all settled before the run gives its lease back."""

import asyncio
from typing import TYPE_CHECKING, Final

from threads.agents import stops
from threads.agents.handoff import handoff
from threads.agents.results import Parked
from threads.agents.scope import Scope
from threads.agents.spawn import Tasks, busy, once, spawn, start_background
from threads.agents.team import deliver, team_tool
from threads.log import AgentSpawnedEvent, ParkAddress
from threads.loop.drafts import draft
from threads.loop.drive import parked
from threads.loop.history import CallState, open_cancel
from threads.loop.runtime import Halt, Idle, Runtime, lost
from threads.reduce.handlers import to_json
from threads.result import Err
from threads.tools import TEAM

if TYPE_CHECKING:
    from pydantic import JsonValue

NAMES: Final = TEAM | {"spawn_agent", "handoff"}


class Agents[D]:
    def __init__(self, scope: Scope[D]) -> None:
        self._scope = scope
        self._tasks: Tasks = {}

    @property
    def names(self) -> frozenset[str]:
        return NAMES

    async def run(self, rt: Runtime, state: CallState) -> Halt | None:
        match state.call.data.name:
            case "spawn_agent":
                return await spawn(self._scope, rt, state, self._tasks)
            case "handoff":
                return await handoff(self._scope, rt, state)
            case _:
                scope = self._scope
                return await team_tool(scope.lead(rt), scope.member(), rt, state)

    async def flush(self, rt: Runtime) -> Halt | None:
        """At a step boundary: first every child the branch is parked on runs again, and one
        that no longer ends parked is resumed; then a background child a crash left running is
        restarted."""
        for at in [a for a in rt.fold.parked if a.kind == "child"]:
            spawned = next(
                e
                for e in rt.events
                if isinstance(e, AgentSpawnedEvent) and e.data.child_thread_id == at.id
            )
            barrier = open_cancel(rt.events)
            if barrier is not None:
                await stops.bar(self._scope, spawned, barrier)
            again = await once(self._scope, rt, spawned)
            if isinstance(again, Parked):
                continue
            halt = busy(again)
            if halt is not None:
                return halt
            data: dict[str, JsonValue] = {
                "address": to_json(at),
                "cause_event_id": spawned.event_id,
            }
            done = await rt.append(draft("resumed", data))
            if isinstance(done, Err):
                return lost(done.error)
        self._restart(rt)
        return None

    def _restart(self, rt: Runtime) -> None:
        """A background child the log says was spawned and never finished, that nothing in
        this process runs and the branch isn't parked on."""
        for child, finished in rt.fold.children.items():
            parked = ParkAddress(kind="child", id=child) in rt.fold.parked
            if finished or parked or child in self._tasks:
                continue
            spawned = next(
                e
                for e in rt.events
                if isinstance(e, AgentSpawnedEvent) and e.data.child_thread_id == child
            )
            if spawned.data.mode == "background":
                start_background(self._scope, rt, spawned, self._tasks)

    async def deliver(self, rt: Runtime) -> Halt | bool:
        scope = self._scope
        if scope.team is None and not scope.definition.subagents:
            return False
        return await deliver(scope.lead(rt), scope.member(), rt)

    async def finish(self, rt: Runtime, halt: Halt) -> Halt:
        """Every background child reaches its result, or parks, before the run ends (v0.1 has
        no detached work), under this lease. One that parked after the turn ended parks the
        run too."""
        self._restart(rt)
        while self._tasks:
            pending = list(self._tasks.values())
            self._tasks.clear()
            await asyncio.gather(*pending)
        if isinstance(halt, Idle) and rt.fold.parked:
            return parked(rt.events, rt.fold.parked)
        return halt
