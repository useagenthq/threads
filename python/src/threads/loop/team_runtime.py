"""What the loop of a team's lead or member needs from its team (spec/schema/README.md, "Teams"),
bound by the agents layer."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import JsonValue

from threads.log import Budget, Event, ModelRef, Policy, Principal
from threads.loop.covering import Covering
from threads.team.batch import Mint
from threads.team.dynamic import Choice, Template
from threads.team.ops import TeamLimits

if TYPE_CHECKING:
    from threads.loop.runtime import Appended, Runtime
    from threads.store import Draft
    from threads.team.rows import MemberRow


@dataclass(frozen=True, slots=True)
class TeamAgentPin:
    """An agent a lead's team lists, pinned as a member."""

    config_hash: str
    config: bytes
    """The canonical config config_hash names, stored before the start that pins it."""
    model: ModelRef
    """What one request of it reserves: its model ref, settings and pinned policy."""
    params: Mapping[str, JsonValue]
    policy: Policy | None
    budget: Budget | None
    """Its own budget: it covers the member."""
    template: Template | None = None
    """A dynamic agent's base pin: what a start may choose (its tools but F, and its keys)."""
    specs: tuple[bytes, ...] = ()
    """Its deferred tools' spec artifacts, stored with the config."""


@dataclass(frozen=True, slots=True)
class TeamRecipient:
    """What covers an asked member (ask's headroom): its own and its ancestors' budgets, and what
    one request of its model reserves. The ask's run budget is the asker's turn's."""

    model: ModelRef
    params: Mapping[str, JsonValue]
    policy: Policy | None
    covering: tuple[Covering, ...]


@dataclass(frozen=True, slots=True)
class TeamRuntime:
    pin: Callable[[str, Choice | None], Awaitable[TeamAgentPin | None]]
    """The agents start may name, pinned on first use (a dynamic agent's with a start's
    choice); None for an agent the team lacks. Raises ConfigError."""
    limits: TeamLimits
    settle: "Callable[[Runtime, Sequence[Draft]], Awaitable[Appended]]"
    """A turn end's append, with the member's settlement in it (threads.loop.teams.settled)."""
    notify: Callable[[], None]
    """Called after each append of a team thread: the team worker looks for work."""
    progress: Callable[[], Awaitable[None]] | None = None
    """The lead of an in-process run waits here for its members' progress until its run ends;
    None: a thread takes its pending mail and stops once idle (a member run by the worker)."""
    principal: Principal | None = None
    """A member's: the one principal its run acts under. Its consume takes only mail sent under
    it; the worker runs the member again under the principal of the mail left pending."""
    busy: Callable[[], bool] | None = None
    """Whether the team worker has a member run in flight: a lead parked on its members waits."""
    run_covering: Callable[[Event], Awaitable[Covering | None]] | None = None
    """A member's turn is under the run budget of the root request its opener belongs to."""
    mint: Mint | None = None
    """The event ids of team appends; tests inject deterministic ones."""
    recipient: "Callable[[MemberRow], Awaitable[TeamRecipient | None]] | None" = None
    """An asked member's budgets, read from its log (or its pinned config while starting); None:
    no budget is known, and ask's headroom holds."""
