"""What the framework tools of one run need from it: the definition, the principal the run acts
for, how to run another thread, and where this thread's sandbox and team live."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from threads.agents.definition import Definition
from threads.agents.launch import Launch, Team
from threads.agents.results import RunResult
from threads.agents.store import Store
from threads.log import Permissions, Principal
from threads.loop.runtime import Runtime
from threads.store import SqliteStore
from threads.tools import SandboxTools

type Execute = Callable[[Definition[None], str, Launch], Awaitable[RunResult[str]]]
"""Runs a launched thread to its result, under its own writer and lease."""


@dataclass(frozen=True, slots=True)
class Scope[D]:
    definition: Definition[D]
    principal: Principal
    store: Store
    sq: SqliteStore
    execute: Execute
    ceilings: tuple[Permissions, ...] = ()
    """What already caps this run: a child's ceilings are these plus this run's own policy."""
    shared: SandboxTools | None = None
    """This run's sandbox tools, shared with children that ask for shared_sandbox."""
    team: Team | None = None
    """Set on a member: its lead. A lead is its own team."""

    def lead(self, rt: Runtime) -> Runtime:
        """The runtime whose writer holds the team's state."""
        return rt if self.team is None else self.team.lead

    def member(self) -> str:
        return self.definition.name if self.team is None else self.team.member

    def team_names(self) -> tuple[str, ...]:
        """Every name a team message may be sent to: the lead's, then its subagents'."""
        if self.team is not None:
            return self.team.names
        return (self.definition.name, *(d.name for d in self.definition.subagents))
