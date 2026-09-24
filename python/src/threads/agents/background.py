"""Background children (F7.2): each runs beside the parent's loop and hands its end over; the
loop records it at a step boundary, so a late result never lands inside a step. A result
recorded while no turn is open, the thread is not cancelled and its log has not ended wakes the
parent in the same append (spec/schema/README.md, "Background wakes"; rules 32 and 45)."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from pydantic import JsonValue

from threads.log import AgentSpawnedEvent, CallId, ThreadId, UserInputEvent
from threads.loop.drafts import draft
from threads.loop.runtime import Failed as HaltFailed
from threads.loop.runtime import Halt, Runtime, lost
from threads.reduce.fold import Fold, Run
from threads.reduce.handlers import to_json
from threads.result import Err
from threads.store import Draft
from threads.store.lines import uuid7

type Ended = tuple[dict[str, JsonValue], dict[str, JsonValue]]
"""A child's agent_finished data and its call's tool_result_late data."""


@dataclass(slots=True)
class Background:
    """The children running in this process, and the ends the loop has not recorded yet."""

    running: dict[ThreadId, asyncio.Task[None]] = field(
        default_factory=dict[ThreadId, "asyncio.Task[None]"]
    )
    ended: dict[ThreadId, tuple[AgentSpawnedEvent, Ended | HaltFailed]] = field(
        default_factory=dict[ThreadId, tuple[AgentSpawnedEvent, Ended | HaltFailed]]
    )

    def has(self, child: ThreadId) -> bool:
        return child in self.running or child in self.ended

    async def next_end(self) -> None:
        """Waits until a running child ends. A child that raised (the process is going down)
        ends the run with it."""
        done, _ = await asyncio.wait(self.running.values(), return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()


async def settle(rt: Runtime, bg: Background) -> Halt | None:
    """Records the ended children this boundary may record, in one append. A child that could
    not run halts this run; it is launched again on the next one."""
    for child, (_, end) in list(bg.ended.items()):
        if isinstance(end, HaltFailed):
            del bg.ended[child]
            return end
    ready = _recordable(rt, bg)
    if not ready:
        return None
    drafts: list[Draft] = []
    for spawned, (finished, late) in ready:
        del bg.ended[spawned.data.child_thread_id]
        drafts.append(draft("agent_finished", finished))
        drafts.append(replace(draft("tool_result_late", late), event_id=uuid7(rt.clock())))
    wake = _wake(rt, ready[0][0], [d.event_id for d in drafts if d.event_id is not None])
    done = await rt.append(*drafts, *([] if wake is None else [wake]))
    return lost(done.error) if isinstance(done, Err) else None


def _recordable(rt: Runtime, bg: Background) -> list[tuple[AgentSpawnedEvent, Ended]]:
    """The writer rule: results of one run at a time, in spawn order. A result of another run
    than the open turn's (or, with no turn open, than the first run to report) waits in
    `bg.ended` until no turn is open."""
    done = sorted(
        ((spawned, end) for spawned, end in bg.ended.values() if not isinstance(end, HaltFailed)),
        key=lambda item: item[0].seq,
    )
    fold = rt.fold
    if not done:
        return []
    if not fold.in_turn and fold.cancelled:
        return done
    runs = fold.wake.spawn_runs

    def run_of(spawned: AgentSpawnedEvent) -> Run | None:
        return runs.get(CallId(spawned.data.call_id))

    target = fold.wake.run if fold.in_turn else run_of(done[0][0])
    return [(spawned, end) for spawned, end in done if run_of(spawned) == target]


def may_wake(fold: Fold) -> bool:
    """Whether a late result recorded now also wakes the thread: no turn is open, the thread is
    not cancelled and its log has not ended (no member_ended)."""
    ended = any(e.type == "member_ended" for e in fold.events)
    return not fold.in_turn and not fold.cancelled and not ended


def _wake(rt: Runtime, spawned: AgentSpawnedEvent, causes: Sequence[str]) -> Draft | None:
    """The woken for late results recorded while the thread may wake, acting for the principal
    of the run that spawned the children."""
    fold = rt.fold
    if not may_wake(fold) or not causes:
        return None
    run = fold.wake.spawn_runs.get(CallId(spawned.data.call_id))
    opener = next(
        (e for e in fold.events if run is not None and e.event_id == run.root),
        None,
    )
    if not isinstance(opener, UserInputEvent):
        return None
    actor: dict[str, JsonValue] = {"kind": "host", "principal": to_json(opener.actor.principal)}
    return Draft("woken", {"causes": list(causes)}, actor)
