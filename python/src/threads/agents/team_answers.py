"""The results of the team tools ask, reply, wait and monitor (spec/api.json AskResult, ReplyResult,
WaitResult, MonitorResult) and the member results they carry: each call's tool_result.preview is
one of these as RFC 8785 JSON."""

from dataclasses import dataclass
from typing import Literal

from threads.agents.team_tools import SendRefusal
from threads.log import BudgetExceededData, MemberErrorCode, MemberRef, ParkReason

type AskRefusal = SendRefusal | Literal["budget_exceeded"]
"""Why an ask was refused: a send's refusals, or no headroom for one request of its model."""
type ReplyRefusal = Literal["unknown_ask", "already_replied", "ask_closed"]
"""Why a reply was refused."""
type ObserveRefusal = Literal["forbidden", "unknown_member", "stale_member"]
"""Why a wait or monitor was refused."""
type CancelRefusal = Literal["forbidden", "unknown_member", "stale_member", "member_ended"]
"""Why a cancel was refused."""


@dataclass(frozen=True, slots=True)
class MemberError:
    code: MemberErrorCode
    message: str


@dataclass(frozen=True, slots=True)
class MemberCompleted:
    """The latest task is done and the member is idle; structured output is its JSON text."""

    member: MemberRef
    output: str
    status: Literal["completed"] = "completed"


@dataclass(frozen=True, slots=True)
class MemberFailed:
    member: MemberRef
    error: MemberError
    status: Literal["failed"] = "failed"


@dataclass(frozen=True, slots=True)
class MemberCancelled:
    member: MemberRef
    status: Literal["cancelled"] = "cancelled"


@dataclass(frozen=True, slots=True)
class MemberBudgetExhausted:
    member: MemberRef
    budget: BudgetExceededData
    status: Literal["budget_exhausted"] = "budget_exhausted"


@dataclass(frozen=True, slots=True)
class MemberHandedOff:
    """The lead handed the conversation off, which closes its team."""

    member: MemberRef
    to_thread: str
    status: Literal["handed_off"] = "handed_off"


type MemberResult = (
    MemberCompleted | MemberFailed | MemberCancelled | MemberBudgetExhausted | MemberHandedOff
)
"""What a settled member returned; a large output is read back from the artifact store."""


@dataclass(frozen=True, slots=True)
class Answered:
    ask_id: str
    text: str
    member: MemberRef
    status: Literal["answered"] = "answered"


@dataclass(frozen=True, slots=True)
class AskTimedOut:
    ask_id: str
    status: Literal["timed_out"] = "timed_out"


@dataclass(frozen=True, slots=True)
class AskMemberEnded:
    ask_id: str
    result: MemberResult
    status: Literal["member_ended"] = "member_ended"


@dataclass(frozen=True, slots=True)
class AskCancelled:
    """The asker's turn or the whole team was cancelled."""

    ask_id: str
    status: Literal["cancelled"] = "cancelled"


@dataclass(frozen=True, slots=True)
class AskNeedsInput:
    """A remote member needs input (Phase 4)."""

    ask_id: str
    prompt: str
    status: Literal["needs_input"] = "needs_input"


@dataclass(frozen=True, slots=True)
class AskUncertain:
    """A remote member's outcome is unknown, and the ask parks (Phase 4)."""

    ask_id: str
    status: Literal["uncertain"] = "uncertain"


type AskOutcome = (
    Answered | AskTimedOut | AskMemberEnded | AskCancelled | AskNeedsInput | AskUncertain
)
"""How an ask ended."""


@dataclass(frozen=True, slots=True)
class AskRefused:
    code: AskRefusal
    status: Literal["refused"] = "refused"


type AskResult = AskOutcome | AskRefused
"""The model's ask tool result."""


@dataclass(frozen=True, slots=True)
class Replied:
    """The reply is durable and pending for the asker."""

    id: str
    status: Literal["sent"] = "sent"


@dataclass(frozen=True, slots=True)
class ReplyRefused:
    code: ReplyRefusal
    status: Literal["refused"] = "refused"


type ReplyResult = Replied | ReplyRefused
"""The model's reply tool result."""


@dataclass(frozen=True, slots=True)
class ParkedMember:
    member: MemberRef
    reason: ParkReason


@dataclass(frozen=True, slots=True)
class Waited:
    """A finished wait: what settled, what is parked, what is still pending."""

    status: Literal["waited"]
    finished: tuple[MemberResult, ...]
    parked: tuple[ParkedMember, ...]
    pending: tuple[MemberRef, ...]
    timed_out: bool
    """The deadline passed with the mode unmet."""


@dataclass(frozen=True, slots=True)
class ObserveRefused:
    code: ObserveRefusal
    status: Literal["refused"] = "refused"


type WaitResult = Waited | ObserveRefused
"""The model's wait tool result."""


@dataclass(frozen=True, slots=True)
class Monitoring:
    """A message arrives when the member ends."""

    member: MemberRef
    status: Literal["monitoring"] = "monitoring"


@dataclass(frozen=True, slots=True)
class MonitoredEnded:
    """The member had already ended."""

    result: MemberResult
    status: Literal["ended"] = "ended"


type MonitorResult = Monitoring | MonitoredEnded | ObserveRefused
"""The model's monitor tool result."""


@dataclass(frozen=True, slots=True)
class CancelRequested:
    """The cancel is accepted and durable, not yet applied; the member ends cancelled at its next
    step."""

    member: MemberRef
    status: Literal["cancel_requested"] = "cancel_requested"


@dataclass(frozen=True, slots=True)
class CancelRefused:
    code: CancelRefusal
    status: Literal["refused"] = "refused"


type CancelResult = CancelRequested | CancelRefused
"""The model's cancel tool result."""
