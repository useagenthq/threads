"""The fold over a resolved chain: what `validate_next` checks against and `reduce` reads.

A `Fold` is a builder. It is mutated in place while one log is folded, one event at a time, and
only by this package. Everything it hands out (`ReducedState`, projections) is immutable.
"""

from dataclasses import dataclass, field
from typing import Literal

from threads.log import (
    BranchId,
    CallId,
    CompactionRequestedEvent,
    ErrorCode,
    Event,
    EventId,
    ModelResponseData,
    ModelResponseRecoveredData,
    ParkAddress,
    ParseError,
    PermissionMode,
    Policy,
    ThreadId,
    ThreadStartedData,
    ToolCallEvent,
    ToolResultData,
    ToolResultLateData,
    ToolSpec,
    UnknownEvent,
    UserInputEvent,
)

type EffectStatus = Literal["begun", "committed", "unknown", "resolved"]
type TaskStatus = Literal["open", "claimed", "completed", "failed"]
type Response = ModelResponseData | ModelResponseRecoveredData
type Result = ToolResultData | ToolResultLateData


@dataclass(frozen=True, slots=True)
class Task:
    status: TaskStatus
    blocked_by: tuple[str, ...]
    owner: str | None = None


type PrincipalKey = tuple[str, str, str]


@dataclass(frozen=True, slots=True)
class Run:
    """The principal and root request of the turn opener that started a run."""

    principal: PrincipalKey
    root: EventId


@dataclass(slots=True)
class Wake:
    run: Run | None = None
    """The run of the open (or last) turn."""
    spawn_runs: dict[CallId, Run] = field(default_factory=dict[CallId, Run])
    """The run that spawned each background spawn_agent call."""
    trailing: dict[EventId, CallId] = field(default_factory=dict[EventId, CallId])
    """tool_result_late ids (to call ids) since the last event that was neither a late result
    nor an agent_finished: the late results of one append."""


@dataclass(slots=True)
class Fold:
    now: int
    """The injected clock (epoch ms); snapshot expiry is judged against it."""
    thread_id: ThreadId | None = None
    segment: BranchId | None = None
    """The branch whose segment is being folded: every event must carry it (semantic rule 4)."""
    seq: int = 0
    epoch: int = 0
    event_ids: set[EventId] = field(default_factory=set[EventId])
    events: list[Event] = field(default_factory=list[Event])
    """Known events of the resolved chain, in order. Unknown non-critical events are skipped."""
    started: ThreadStartedData | None = None
    tools: dict[str, ToolSpec] = field(default_factory=dict[str, ToolSpec])
    in_turn: bool = False
    turns: int = 0
    handed_off: bool = False
    over_budget: bool = False
    open_requests: set[EventId] = field(default_factory=set[EventId])
    compaction_requests: set[EventId] = field(default_factory=set[EventId])
    causes: dict[EventId, EventId] = field(default_factory=dict[EventId, EventId])
    """Side request -> the compaction_requested it names."""
    compaction_request: CompactionRequestedEvent | None = None
    """The request no compacted or compaction_failed has answered yet (rule 30)."""
    first_input: UserInputEvent | None = None
    """The first user_input of the resolved chain: where a requested compaction starts."""
    responses: dict[EventId, Response] = field(default_factory=dict[EventId, Response])
    calls: dict[CallId, ToolCallEvent] = field(default_factory=dict[CallId, ToolCallEvent])
    pending: list[CallId] = field(default_factory=list[CallId])
    read_only_calls: set[CallId] = field(default_factory=set[CallId])
    allowed: set[CallId] = field(default_factory=set[CallId])
    challenges: dict[str, tuple[CallId, str]] = field(default_factory=dict[str, tuple[CallId, str]])
    """Open approval challenges: challenge_id -> (call_id, args_hash)."""
    consumed: set[str] = field(default_factory=set[str])
    effects: dict[str, tuple[CallId, EffectStatus]] = field(
        default_factory=dict[str, tuple[CallId, EffectStatus]]
    )
    results: dict[CallId, Result] = field(default_factory=dict[CallId, Result])
    deferred: set[CallId] = field(default_factory=set[CallId])
    last_cancel_seq: int = 0
    cancel_scopes: dict[EventId, str] = field(default_factory=dict[EventId, str])
    cancelled: bool = False
    parked: list[ParkAddress] = field(default_factory=list[ParkAddress])
    fork_points: list[tuple[int, EventId]] = field(default_factory=list[tuple[int, EventId]])
    boundaries: set[int] = field(default_factory=lambda: {0})
    """Seqs after which no tool call is pending and no model attempt awaits its response."""
    ranges: list[tuple[int, int]] = field(default_factory=list[tuple[int, int]])
    repair: bool = False
    unique_keys: set[tuple[str, str]] = field(default_factory=set[tuple[str, str]])
    """Branch-unique values tagged by kind: item keys, occurrence ids, message ids."""
    mode: PermissionMode = "default"
    children: dict[ThreadId, bool] = field(default_factory=dict[ThreadId, bool])
    tasks: dict[str, Task] = field(default_factory=dict[str, Task])
    input_tokens: int = 0
    output_tokens: int = 0
    unknown_responses: int = 0
    wake: Wake = field(default_factory=Wake)
    """Background wake bookkeeping (rules_wake)."""


def policy(fold: Fold) -> Policy | None:
    """The runtime policy pinned by thread_started; None means the ADR defaults apply."""
    started = fold.started
    return started.policy if started is not None and isinstance(started.policy, Policy) else None


def reject(
    event: Event | UnknownEvent, message: str, code: ErrorCode = "invalid_transition"
) -> ParseError:
    return ParseError(code, message, event.seq)
