"""The framework tools of one run, as the loop's `Framework`: spawn_agent, handoff and
the team tools each advance their call on the log; background children run beside the loop,
their ends are recorded at step boundaries (waking the idle lead), and the run waits for them
before it gives its lease back."""

from typing import TYPE_CHECKING, Final

from threads.agents import stops
from threads.agents.background import Background, settle
from threads.agents.handoff import handoff
from threads.agents.members import send_call, start_call
from threads.agents.results import Parked
from threads.agents.scope import Scope
from threads.agents.spawn import busy, once, spawn, start_background
from threads.agents.stop import run_status, stop_children
from threads.agents.team import deliver, team_tool
from threads.log import AgentSpawnedEvent, ParkAddress
from threads.loop import runtime
from threads.loop.drafts import draft
from threads.loop.drive import drive, parked
from threads.loop.history import CallState, open_cancel
from threads.loop.runtime import Halt, Idle, Runtime, lost
from threads.reduce.fold import loop_parked
from threads.reduce.handlers import to_json
from threads.reduce.run_end import ended_otherwise
from threads.result import Err
from threads.tools import TEAM
from threads.tools.specs import PINNED_MEMBERS

if TYPE_CHECKING:
    from pydantic import JsonValue

NAMES: Final = TEAM | PINNED_MEMBERS | {"spawn_agent", "handoff"}


class Agents[D]:
    def __init__(self, scope: Scope[D], *, team: bool = False) -> None:
        self._scope = scope
        self._bg = Background()
        # Outside a team, send and start are free names: an app tool may take one.
        self._names = NAMES if team else NAMES - PINNED_MEMBERS

    @property
    def names(self) -> frozenset[str]:
        return self._names

    async def run(self, rt: Runtime, state: CallState) -> Halt | None:
        match state.call.data.name:
            case "spawn_agent":
                return await spawn(self._scope, rt, state, self._bg)
            case "handoff":
                return await handoff(self._scope, rt, state)
            case "start":
                return await start_call(rt, state)
            case "send":
                return await send_call(rt, state)
            case _:
                scope = self._scope
                team = (scope.lead(rt), scope.member(), scope.team_names())
                return await team_tool(team, rt, state)

    async def flush(self, rt: Runtime) -> Halt | None:
        """At a step boundary: first every child the branch is parked on runs again, and one
        that no longer ends parked is resumed; then a background child a crash left running is
        restarted, and the background ends this boundary may record are recorded."""
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
        return await settle(rt, self._bg)

    def _restart(self, rt: Runtime) -> None:
        """A background child the log says was spawned and never finished, that nothing in
        this process runs and the branch isn't parked on."""
        for child, finished in rt.fold.children.items():
            parked = ParkAddress(kind="child", id=child) in rt.fold.parked
            if finished or parked or self._bg.has(child):
                continue
            spawned = next(
                e
                for e in rt.events
                if isinstance(e, AgentSpawnedEvent) and e.data.child_thread_id == child
            )
            if spawned.data.mode == "background":
                start_background(self._scope, rt, spawned, self._bg)

    async def deliver(self, rt: Runtime) -> Halt | bool:
        scope = self._scope
        if scope.team is None and not scope.definition.subagents:
            return False
        return await deliver(scope.lead(rt), scope.member(), rt)

    async def finish(self, rt: Runtime, halt: Halt) -> Halt:
        """The run waits for its background children under this lease (spec/schema/README.md,
        "Run completion"): each end is recorded, and one recorded while no turn is open wakes
        the lead for another turn. A cancelled run returns once its own log records the cancel;
        one that ended failed, budget_exhausted or handed_off first stops its own children and
        records their ends. A parked run stops once nothing more is running; an end held for
        another run waits for the next run. One that parked after the turn ended parks the run
        too."""
        bg = self._bg
        while isinstance(halt, Idle | runtime.Parked):
            status = run_status(rt.events)
            if status == "cancelled":
                break
            if ended_otherwise(status):
                halt = await stop_children(self._scope, rt, bg, halt)
                break
            self._restart(rt)
            ends = isinstance(halt, Idle) and bool(bg.ended)
            if not ends and not bg.running:
                break
            if not ends:
                # The lead's own log moving (a cancel) wakes the wait too, even while a child
                # hangs.
                await bg.next_end(rt.writer.moved())
            halt = await drive(rt)
        if isinstance(halt, Idle) and loop_parked(rt.fold):
            return parked(rt.events, loop_parked(rt.fold))
        return halt
