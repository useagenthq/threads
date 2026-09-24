"""Opening a run's thread: pin a fresh thread's config, or recover a
continued one; then record the input. A launched thread (a subagent child or a handoff target)
is pinned with its parent link, and gets only the inputs its parent sent that it lacks."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from threads.agents.config import ConfigError
from threads.agents.definition import Definition
from threads.agents.handoff import launch as handoff_launch
from threads.agents.handoff import target
from threads.agents.intake import Intake
from threads.agents.launch import Launch, open_launched
from threads.agents.results import Failed, HandedOff, RunError, RunResult, Thread
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
from threads.loop.runtime import LOST, Barred, Halt, Idle, Runtime, lost
from threads.loop.runtime import Failed as HaltFailed
from threads.redaction import contains_secret
from threads.reduce.handlers import to_json
from threads.result import Err, Ok
from threads.store import Draft, SqliteStore, Writer

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
    if contains_secret(config):
        # The config artifact is byte-exact (config_hash names it): one holding a registered
        # value is never pinned.
        raise ConfigError("invalid_config", "the resolved config holds a registered secret")
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


def pinned_config(events: Iterable[object]) -> str | None:
    """The config_hash a thread's thread_started pins; None before it started."""
    started = next((e for e in events if isinstance(e, ThreadStartedEvent)), None)
    return None if started is None else started.data.config_hash


def same_pin(events: Iterable[object], started: Draft) -> bool:
    """Whether a thread pins the config the draft thread_started `started` would: false when
    either has no pin. The comparison every "can this agent run this thread" check uses."""
    pinned = pinned_config(events)
    return pinned is not None and pinned == started.data.get("config_hash")


def _check_pin(rt: Runtime, config_hash: object) -> None:
    """A pin never changes in place: continuing a thread needs the config it started with, checked
    before recovery can dispatch anything."""
    pinned = pinned_config(rt.events)
    if pinned is not None and pinned != config_hash:
        raise ConfigError(
            "invalid_config",
            "this thread was started with another config; a config change starts a new thread",
        )


def wants_input(rt: Runtime, launch: Launch | None) -> bool:
    """A launched thread takes an input only when it lacks one its parent sent."""
    if launch is None:
        return True
    return sum(1 for e in rt.events if isinstance(e, UserInputEvent)) < launch.inputs


@dataclass(frozen=True, slots=True)
class Recorded:
    """A run's input and how it came: its sender, run budget, launch and host intake."""

    input: Input | None
    """None: nothing to record; the run continues the thread."""
    principal: Principal
    budget: Budget | None
    launch: Launch | None
    intake: Intake | None = None


async def record_input(rt: Runtime, recorded: Recorded) -> Halt | None:
    """The input, recorded with the principal that sent it. A host intake's delivery event and
    host rows go in the same append; a refused companion records nothing."""
    input, principal, budget, launch, intake = (
        recorded.input,
        recorded.principal,
        recorded.budget,
        recorded.launch,
        recorded.intake,
    )
    source = intake.source if intake is not None else "api" if launch is None else launch.source
    data: dict[str, JsonValue] = {"source": source}
    if input is None:
        raise AssertionError("a run without an input records none")
    if isinstance(input, str):
        data["text"] = input
    else:
        data["content"] = [to_json(part) for part in input]
    if budget is not None:
        data["budget"] = to_json(budget)
    if intake is not None and intake.delivery_event_id is not None:
        data["delivery_event_id"] = intake.delivery_event_id
    actor: dict[str, JsonValue] = {"kind": "user", "principal": to_json(principal)}
    drafts = [
        *(() if intake is None else intake.before),
        replace(draft("user_input", data), actor=actor),
    ]
    done = await rt.append_with(drafts, None if intake is None else intake.companion)
    if isinstance(done, Err):
        if intake is not None and done.error.code not in LOST:
            return HaltFailed("branch_not_runnable", done.error.message)
        return lost(done.error)
    if isinstance(done, Barred):
        raise AssertionError("an input opens no work the cancel barrier could refuse")
    if intake is not None:
        intake.recorded.set_result(done.value[-1])
    return None


async def handed_off[D](
    scope: Scope[D], rt: Runtime, thread: Thread, *, again: bool
) -> RunResult[str]:
    """Starts (or, after a crash, finishes starting) the handoff target, which answers the
    pending request; the run's result names both threads. A thread that had already handed off
    takes no new input (semantic rule 26): that run fails and names where the conversation
    went."""
    event = next(e for e in reversed(rt.events) if isinstance(e, HandoffEvent))
    chosen = target(scope, event.data.to_agent)
    if chosen is None:
        raise AssertionError("a recorded handoff names a configured target")
    how, text = handoff_launch(scope, rt, event)
    answered = await scope.execute(chosen, text, how)
    if again:
        why = f"this thread handed off to {event.data.to_thread_id}; continue that one"
        return Failed(RunError("branch_not_runnable", why), thread)
    return HandedOff(thread, answered.thread)
