"""The results and reads of the operator's handle on a team (spec/api.json Team* results,
TeamMember, TeamItem, AskStatus): what Team's methods return."""

from dataclasses import dataclass
from typing import Literal

from threads.agents.member_results import MemberResult
from threads.agents.team_answers import (
    AskOutcome,
    AskRefusal,
    CancelRefusal,
    CancelRequested,
    ObserveRefusal,
    Waited,
)
from threads.agents.team_tools import SendRefusal, Sent, Started, StartRefusal
from threads.log import Event, MemberRef, Principal
from threads.team.dynamic import InvalidDefinition


@dataclass(frozen=True, slots=True)
class TeamRef:
    """Which team: team.ref, for open_team in another process."""

    tenant: str
    id: str


type OperatorRefusal = Literal[
    "busy", "idempotency_key_reused", "idempotency_key_principal_mismatch"
]
"""Refusals only Team methods return. busy: the team log stayed locked past the busy bound, so
nothing was recorded; retrying with the same key is safe."""


@dataclass(frozen=True, slots=True)
class TeamStartRefused:
    code: StartRefusal | OperatorRefusal
    status: Literal["refused"] = "refused"
    detail: InvalidDefinition | None = None
    """Present exactly when code is invalid_definition."""


@dataclass(frozen=True, slots=True)
class TeamSendRefused:
    code: SendRefusal | OperatorRefusal
    status: Literal["refused"] = "refused"


type TeamStartResult = Started | TeamStartRefused
"""team.start's result."""
type TeamSendResult = Sent | TeamSendRefused
"""team.send's result."""


@dataclass(frozen=True, slots=True)
class TeamAskRefused:
    code: AskRefusal | OperatorRefusal
    status: Literal["refused"] = "refused"


type TeamAskResult = AskOutcome | TeamAskRefused
"""team.ask's result: how the ask ended, or why it was refused."""


@dataclass(frozen=True, slots=True)
class TeamWaitRefused:
    """invalid_request (a numeric mode above the member count) is returned before any writer, so,
    like busy, it is never logged."""

    code: ObserveRefusal | OperatorRefusal | Literal["invalid_request"]
    status: Literal["refused"] = "refused"


type TeamWaitResult = Waited | TeamWaitRefused
"""team.wait's result."""


@dataclass(frozen=True, slots=True)
class TeamCancelRefused:
    code: CancelRefusal | OperatorRefusal
    status: Literal["refused"] = "refused"


type TeamCancelResult = CancelRequested | TeamCancelRefused
"""team.cancel's result: cancel_requested is durable; the member ends at its next step."""


@dataclass(frozen=True, slots=True)
class AskOpen:
    """Waiting for a reply."""

    ask_id: str
    deadline: int
    """When it times out."""
    status: Literal["open"] = "open"


@dataclass(frozen=True, slots=True)
class AskNotFound:
    """No such ask in this team's log."""

    ask_id: str
    status: Literal["not_found"] = "not_found"


type AskStatus = AskOpen | AskNotFound | AskOutcome
"""team.ask_status's answer: open, not_found, or how the ask ended."""

type MemberState = Literal["starting", "running", "idle", "parked", "ended"]
"""starting: its log not opened yet. idle: its latest task is done; mail wakes it."""


@dataclass(frozen=True, slots=True)
class TeamMember:
    """One row of team.members()."""

    name: str
    ref: MemberRef
    agent: str
    state: MemberState
    result: MemberResult | None = None
    """Present once idle or ended."""
    label: str | None = None
    """The display label its start gave, if any. Never shown to a model."""


@dataclass(frozen=True, slots=True)
class TeamCursor:
    """A position in the team feed, to resume team.events after."""

    epoch: int
    offset: int


@dataclass(frozen=True, slots=True)
class MemberSource:
    """A member's (or the lead's) own log."""

    member: MemberRef
    kind: Literal["member"] = "member"


@dataclass(frozen=True, slots=True)
class OperatorSource:
    """The team log's events of one operator request."""

    principal: Principal
    request: str
    kind: Literal["operator"] = "operator"


@dataclass(frozen=True, slots=True)
class TeamLogSource:
    """The team log's own team_opened."""

    kind: Literal["team"] = "team"


type TeamSource = MemberSource | OperatorSource | TeamLogSource
"""Which log a feed item came from."""


@dataclass(frozen=True, slots=True)
class TeamEvent:
    """One committed event, exactly as stored (a result keeps its artifact ref)."""

    cursor: TeamCursor
    source: TeamSource
    event: Event
    kind: Literal["event"] = "event"


@dataclass(frozen=True, slots=True)
class EpochRestarted:
    """The feed was rebuilt; items restart from the new epoch."""

    cursor: TeamCursor
    kind: Literal["epoch_restarted"] = "epoch_restarted"


type TeamItem = TeamEvent | EpochRestarted
"""One item of team.events()."""
