"""What the team worker runs a member with: one member branch, and the worker's environment.
Its own module so a member path can take them without importing the worker (a cycle)."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from threads.agents.definition import Definition
from threads.agents.store import Store
from threads.log import BranchId, Parent, Principal, ThreadId
from threads.loop.covering import Covering
from threads.loop.team_runtime import TeamAgentPin
from threads.store import SqliteStore
from threads.team.batch import Mint
from threads.team.constants import TEAM_CONSTANTS


@dataclass(frozen=True, slots=True)
class MemberRun:
    """One member branch, as the worker runs it."""

    thread: ThreadId
    branch: BranchId
    parent: Parent | None
    """None for a host member (Teams Phase 2): a root thread, under no ancestor's budget."""
    principal: Principal
    """The principal of the member's task: its turns' actor."""
    holder: str
    notify: Callable[[], None]
    covering: tuple[Covering, ...]
    """Every ancestor's budget: it covers the member too."""
    abort: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when the worker stops this run for a cancel: its model call ends at once."""
    host_member: bool = False
    """A host member (Teams Phase 2): pinned with reply and its rules' tools, not all seven."""


@dataclass(frozen=True, slots=True)
class WorkerEnv:
    store: Store
    sq: SqliteStore
    team: Callable[[], str | None]
    """The lead's team: None until its first append names it."""
    agents: Mapping[str, Definition[None]]
    """Every agent of the lead's team tree, by name."""
    pin: Callable[[Definition[None]], Awaitable[TeamAgentPin]]
    run: Callable[[Definition[None], MemberRun], Awaitable[None]]
    mint: Mint | None = None
    claim_ttl_ms: int = TEAM_CONSTANTS.claim_ttl_ms
    setup_attempts: int = TEAM_CONSTANTS.setup_attempts
    """How many setup failures in a row end a member setup_failed; tests inject fewer."""
