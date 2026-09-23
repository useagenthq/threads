"""Opening a run's thread: pin a fresh thread's config, or recover a
continued one; then record the input. A launched thread (a subagent child or a handoff target)
is pinned with its parent link, and gets only the inputs its parent sent that it lacks."""

from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from threads.agents.config import ConfigError
from threads.agents.definition import Definition
from threads.agents.handoff import launch as handoff_launch
from threads.agents.handoff import target
from threads.agents.launch import Launch, open_launched
from threads.agents.results import HandedOff, RunResult, Thread
from threads.agents.scope import Scope
from threads.agents.store import now_ms
from threads.log import (
    Budget,
    HandoffEvent,
    InputPart,
    ParseError,
    Principal,
    ThreadStartedEvent,
    UserInputEvent,
)
from threads.loop import gates
from threads.loop.drafts import draft
from threads.loop.drive import drive
from threads.loop.recovery import recover
from threads.loop.runtime import Failed, Halt, Idle, Runtime, lost
from threads.reduce.handlers import to_json
from threads.result import Err, Ok
from threads.store import SqliteStore, Writer

if TYPE_CHECKING:
    from pydantic import JsonValue

type Input = str | Sequence[InputPart]


async def launched(
    sq: SqliteStore, launch: Launch, holder: str
) -> Ok[tuple[Writer, bool]] | Err[ParseError]:
    """A launched thread's branch; fresh until its thread_started is recorded."""
    opened = await open_launched(sq, launch, holder, now_ms)
    if isinstance(opened, Err):
        return opened
    pinned = any(isinstance(e, ThreadStartedEvent) for e in opened.value.fold.events)
    return Ok((opened.value, not pinned))


async def prepare[D](
    rt: Runtime, definition: Definition[D], *, fresh: bool, launch: Launch | None
) -> Halt | None:
    """A new thread pins its config; a continued one first recovers and finishes an open turn."""
    started, config = definition.pin()
    if fresh:
        # The resolved config, hooks included, is durable in the content-addressed store under
        # its config_hash before the pin that names it.
        await rt.store.put_artifact(config)
        if launch is not None:
            started = {**started, "parent": launch.parent}
        before = () if launch is None else launch.before_input
        done = await rt.append(draft("thread_started", started), *before)
        return (
            lost(done.error) if isinstance(done, Err) else await gates.session_start(rt, "startup")
        )
    _check_pin(rt, started["config_hash"])
    halt = await recover(rt)
    if halt is None and rt.fold.in_turn:
        halt = await drive(rt)
    # A turn that ended is out of the way; a park or a failure is this run's result.
    if halt is None or isinstance(halt, Idle):
        return await gates.session_start(rt, "resume")
    return halt


def _check_pin(rt: Runtime, config_hash: object) -> None:
    """A pin never changes in place: continuing a thread needs the config it started with, checked
    before recovery can dispatch anything."""
    pinned = next((e for e in rt.events if isinstance(e, ThreadStartedEvent)), None)
    if pinned is not None and pinned.data.config_hash != config_hash:
        raise ConfigError(
            "invalid_config",
            "this thread was started with another config; a config change starts a new thread",
        )


def wants_input(rt: Runtime, launch: Launch | None) -> bool:
    """A launched thread takes an input only when it lacks one its parent sent."""
    if launch is None:
        return True
    return sum(1 for e in rt.events if isinstance(e, UserInputEvent)) < launch.inputs


async def record_input(
    rt: Runtime, input: Input, principal: Principal, budget: Budget | None, launch: Launch | None
) -> Halt | None:
    """The input and its principal. A thread that handed off takes none (semantic rule 26)."""
    if rt.fold.handed_off:
        return Failed("branch_not_runnable", "this thread handed off; continue the new one")
    source = "api" if launch is None else launch.source
    data: dict[str, JsonValue] = {"source": source}
    if isinstance(input, str):
        data["text"] = input
    else:
        data["content"] = [to_json(part) for part in input]
    if budget is not None:
        data["budget"] = to_json(budget)
    actor: dict[str, JsonValue] = {"kind": "user", "principal": to_json(principal)}
    done = await rt.append(replace(draft("user_input", data), actor=actor))
    return lost(done.error) if isinstance(done, Err) else None


async def handed_off[D](scope: Scope[D], rt: Runtime, thread: Thread) -> RunResult[str]:
    """Starts (or, after a crash, finishes starting) the handoff target, which answers the
    pending request; the run's result names both threads."""
    event = next(e for e in reversed(rt.events) if isinstance(e, HandoffEvent))
    chosen = target(scope, event.data.to_agent)
    if chosen is None:
        raise AssertionError("a recorded handoff names a configured target")
    how, text = handoff_launch(scope, rt, event)
    answered = await scope.execute(chosen, text, how)
    return HandedOff(thread, answered.thread)
