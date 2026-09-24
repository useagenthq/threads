"""The results of the team tools start and send (spec/api.json StartResult, SendResult): each call's
tool_result.preview is one of these as RFC 8785 JSON."""

from dataclasses import dataclass
from typing import Literal

from threads.log import MemberRef
from threads.team.dynamic import InvalidDefinition

type StartRefusal = Literal[
    "forbidden",
    "unknown_agent",
    "concurrency_cap",
    "budget_exceeded",
    "team_closed",
    "invalid_definition",
]
"""Why a start was refused. team_closed: the lead ended, which closes the team.
invalid_definition: a label, instructions, tools or model the start may not choose."""
type SendRefusal = Literal[
    "forbidden",
    "unknown_member",
    "stale_member",
    "member_ended",
    "self",
    "mailbox_full",
    "team_closed",
]
"""Why a send was refused. stale_member: the member was restarted under a newer generation."""


@dataclass(frozen=True, slots=True)
class Started:
    """The member exists in state starting; its task is queued."""

    member: MemberRef
    status: Literal["started"] = "started"


@dataclass(frozen=True, slots=True)
class StartRefused:
    code: StartRefusal
    status: Literal["refused"] = "refused"
    detail: InvalidDefinition | None = None
    """Present exactly when code is invalid_definition: which field, why, what is allowed."""


@dataclass(frozen=True, slots=True)
class Sent:
    """The mail is durable and pending for its recipient."""

    id: str
    status: Literal["sent"] = "sent"


@dataclass(frozen=True, slots=True)
class SendRefused:
    code: SendRefusal
    status: Literal["refused"] = "refused"


type StartResult = Started | StartRefused
type SendResult = Sent | SendRefused
