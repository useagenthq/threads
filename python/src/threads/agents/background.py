"""Background children (F7.2): each runs beside the parent's loop and hands its end over; the
loop records it at a step boundary, so a late result never lands inside a step. A result
recorded while the wake condition holds (rule 32, `wake_bar`) wakes the parent in the same
append (spec/schema/README.md, "Background wakes"; rules 32 and 45)."""

import asyncio
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field, replace

from pydantic import JsonValue

from threads.log import (
    AgentSpawnedEvent,
    MessageReceivedEvent,
    ThreadId,
    UserInputEvent,
    WokenEvent,
)
from threads.loop.drafts import draft
from threads.loop.runtime import Failed as HaltFailed
from threads.loop.runtime import Halt, Runtime, lost
from threads.reduce.fold import Fold
from threads.reduce.handlers import to_json
from threads.reduce.rules_wake import wake_bar
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

    async def next_end(self, *also: Awaitable[object]) -> None:
        """Waits until a running child ends, or one of `also` does. A child that raised (the
        process is going down) ends the run with it."""
        extra = [asyncio.ensure_future(a) for a in also]
        try:
            done, _ = await asyncio.wait(
                [*self.running.values(), *extra], return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for waiter in extra:
                waiter.cancel()
        for task in done:
            if task not in extra:
                task.result()


async def settle(rt: Runtime, bg: Background) -> Halt | None:
    """Records the ended children this boundary may record, in one append. A child that could
    not run halts this run; it is launched again on the next one. An end leaves `bg.ended` only
    once its append committed, so a refused append loses nothing."""
    for child, (_, end) in list(bg.ended.items()):
        if isinstance(end, HaltFailed):
            del bg.ended[child]
            return end
    ready = _recordable(rt.fold, bg)
    if not ready:
        return None
    drafts: list[Draft] = []
    for _, (finished, late) in ready:
        drafts.append(draft("agent_finished", finished))
        drafts.append(replace(draft("tool_result_late", late), event_id=uuid7(rt.clock())))
    calls = [spawned.data.call_id for spawned, _ in ready]
    causes = [d.event_id for d in drafts if d.event_id is not None]

    def build(fold: Fold) -> Sequence[Draft]:
        # Decided under the writer's lock, against the fold this append extends.
        wake = _wake(fold, ready[0][0], calls, causes)
        return drafts if wake is None else [*drafts, wake]

    done = await rt.append_built(build)
    if isinstance(done, Err):
        return lost(done.error)
    for spawned, _ in ready:
        del bg.ended[spawned.data.child_thread_id]
    return None


def _recordable(fold: Fold, bg: Background) -> list[tuple[AgentSpawnedEvent, Ended]]:
    """The writer rule: results of one run at a time, in spawn order. A result of another run
    than the open turn's (or, with no turn open, than the first run to report) waits in
    `bg.ended` until no turn is open."""
    done = sorted(
        ((spawned, end) for spawned, end in bg.ended.values() if not isinstance(end, HaltFailed)),
        key=lambda item: item[0].seq,
    )
    if not done:
        return []
    if not fold.in_turn and fold.cancelled:
        return done
    spawns = fold.team.spawns
    target = fold.team.turn if fold.in_turn else spawns.get(done[0][0].data.call_id)
    return [(s, end) for s, end in done if spawns.get(s.data.call_id) == target]


def _wake(
    fold: Fold, spawned: AgentSpawnedEvent, calls: Sequence[str], causes: Sequence[str]
) -> Draft | None:
    """The woken for these late results, acting for the principal of the run that spawned the
    children: its spawning turn's opener (a user_input, turn-opening mail or woken; mail joins a
    turn only when it shares the turn's run, rule 34). None when the thread must not wake."""
    if not causes or wake_bar(fold, calls) is not None:
        return None
    at = fold.events.index(spawned) if spawned in fold.events else len(fold.events)
    opener = next(
        (
            e
            for e in reversed(fold.events[:at])
            if isinstance(e, UserInputEvent | WokenEvent | MessageReceivedEvent)
        ),
        None,
    )
    if opener is None:
        return None
    actor: dict[str, JsonValue] = {"kind": "host", "principal": to_json(opener.actor.principal)}
    return Draft("woken", {"causes": list(causes)}, actor)
