"""How a thread another thread starts is opened: a subagent child or a handoff target,
created with its id chosen by the parent (so `agent_spawned` or `handoff` names it before it
exists), or reopened by that id when a crash interrupted it."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from threads.log import BranchId, ParseError, Permissions, Principal, ThreadId, UserInputEvent
from threads.loop.covering import Covering
from threads.loop.runtime import Runtime
from threads.loop.stubs import Stub
from threads.result import Err, Ok
from threads.store import SqliteStore, Writer
from threads.store.lines import Draft, uuid7
from threads.store.worker import Clock
from threads.tools import SandboxTools


@dataclass(frozen=True, slots=True)
class Team:
    """A member's route to its team: every team event is appended by the lead's writer, so
    claims are atomic under 's single writer."""

    lead: Runtime
    member: str


@dataclass(frozen=True, slots=True)
class Launch:
    thread_id: ThreadId
    parent: dict[str, JsonValue]
    """thread_started.parent: the parent event that created this thread."""
    source: Literal["parent_agent", "handoff"]
    principal: Principal
    """The originating principal: a child or a handoff target never becomes a new caller."""
    inputs: int
    """How many inputs the parent has sent this thread: a reopened thread missing the last one
    (a crash before it was recorded) gets it, and one that has it gets nothing twice."""
    budgets: tuple[Covering, ...] = ()
    ceilings: tuple[Permissions, ...] = ()
    shared: SandboxTools | None = None
    """The parent's sandbox, for shared_sandbox isolation."""
    before_input: tuple[Draft, ...] = ()
    """A fresh handoff target's forwarded history, recorded before its input."""
    team: Team | None = None
    """How this member reaches its team's lead."""
    stubs: tuple[Stub, ...] | None = None
    """Set when the launching run is in stub mode: so is this thread's run."""


async def open_launched(
    sq: SqliteStore, launch: Launch, holder: str, clock: Clock
) -> Ok[Writer] | Err[ParseError]:
    """The launched thread's root branch, created on first launch."""
    root = await sq.root(launch.thread_id)
    if isinstance(root, Err):
        branch = BranchId(uuid7(clock()))
        created = await sq.create(launch.thread_id, branch, clock())
        if isinstance(created, Err):
            return created
        return await sq.acquire(branch, holder, clock)
    return await sq.acquire(root.value, holder, clock)


def missing_input(launch: Launch, inputs: Sequence[UserInputEvent]) -> bool:
    return len(inputs) < launch.inputs
