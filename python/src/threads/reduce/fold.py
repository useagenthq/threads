"""The fold over a resolved chain: what `validate_next` checks against and `reduce` reads.

A `Fold` is a builder. It is mutated in place while one log is folded, one event at a time, and
only by this package. Everything it hands out (`ReducedState`, projections) is immutable.
"""

from dataclasses import dataclass, field
from typing import Literal

from threads.log import (
    ArtifactRef,
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
    PermissionRuleAddedData,
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


@dataclass(slots=True)
class Wake:
    trailing: dict[EventId, CallId] = field(default_factory=dict[EventId, CallId])
    """tool_result_late ids (to call ids) since the last event that was neither a late result
    nor an agent_finished: the late results of one append."""


@dataclass(frozen=True, slots=True)
class TurnRun:
    """A turn's run: its opener's principal and the whole root request. A member's task turn has
    no root request in its own log (it is in the lead's): None, and only its principal counts."""

    principal: PrincipalKey
    root: tuple[str, str] | None
    """(thread_id, event_id) of the root request."""


@dataclass(slots=True)
class Team:
    """What one log's events leave for semantic rules 31 and 33-45 (team_fold, rules_team). Ids
    are the wire's MailId, AskId, WaitId and MonitorId strings."""

    team_log: bool = False
    """The log starts with team_opened: it takes only operator-side events (rule 33)."""
    lead_thread: str | None = None
    member: bool = False
    """thread_started.parent.relation is team_member (rule 41)."""
    had_input: bool = False
    ended: bool = False
    stopped: bool = False
    """A member's log took a tree cancel: it opens no turn again (rule 37)."""
    last_end: str | None = None
    """The reason of the last turn_completed (rule 38)."""
    turn: TurnRun | None = None
    """The run of the open turn (rule 34)."""
    spawns: dict[str, TurnRun] = field(default_factory=dict[str, TurnRun])
    """The run that spawned each background spawn_agent call: a woken turn's run (rules 32,
    34)."""
    ended_runs: set[TurnRun] = field(default_factory=set[TurnRun])
    """Runs with a turn that ended but end_turn: never woken again (rule 32)."""
    barred: set[str] = field(default_factory=set[str])
    """Background calls a thread or tree cancel request followed: never woken (rule 32)."""
    mail_done: set[str] = field(default_factory=set[str])
    """Mail received, refused or taken as a task (rule 31)."""
    asks_in: set[str] = field(default_factory=set[str])
    """Asks this log received and has not replied to (rule 35)."""
    asks_out: set[str] = field(default_factory=set[str])
    """Asks this log sent and has not closed (rules 36, 40)."""
    replies_in: dict[str, str] = field(default_factory=dict[str, str])
    """Received replies: reply mail_id -> the ask it answers (rule 36)."""
    waits: set[str] = field(default_factory=set[str])
    """Waits this log started and has not finished (rules 39, 40)."""
    monitors: set[str] = field(default_factory=set[str])
    """Monitors this log registered that have neither fired nor been observed (rule 39)."""
    settle: set[str] = field(default_factory=set[str])
    """The settle monitors of this log's waits: their notifications open no turn."""
    task_monitors: set[str] = field(default_factory=set[str])
    """The task monitor of each member_started in this log (rule 40)."""
    requests: set[str] = field(default_factory=set[str])
    """operator_request ids in this log (rule 42)."""
    request_events: set[str] = field(default_factory=set[str])
    """operator_request event ids in this log (rule 42)."""


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
    """The latest set by name; a repeated name keeps its first spec, as TypeScript's lookup."""
    known_tools: dict[str, ToolSpec] = field(default_factory=dict[str, ToolSpec])
    """Each name's spec as pinned by thread_started or first added (rule 17)."""
    loaded: dict[str, ArtifactRef] = field(default_factory=dict[str, ArtifactRef])
    """Reference-form tools a tools_loaded loaded, with their spec_ref (rule 47)."""
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
    host_calls: set[CallId] = field(default_factory=set[CallId])
    """Host-issued channel_send calls (a reply, card, question or correction): the host's
    outbound path settles them, never the agent's loop. Never removed."""
    call_specs: dict[CallId, ToolSpec] = field(default_factory=dict[CallId, ToolSpec])
    """The spec each call was made under; a later tools_changed never reclasses it."""
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
    thread_rules: list[PermissionRuleAddedData] = field(
        default_factory=list[PermissionRuleAddedData]
    )
    """Rules remembered on the thread (permission_rule_added), in log order."""
    children: dict[ThreadId, bool] = field(default_factory=dict[ThreadId, bool])
    tasks: dict[str, Task] = field(default_factory=dict[str, Task])
    input_tokens: int = 0
    output_tokens: int = 0
    unknown_responses: int = 0
    wake: Wake = field(default_factory=Wake)
    """Background wake bookkeeping (rules_wake)."""
    team: Team = field(default_factory=Team)
    """Team bookkeeping (team_fold)."""


def loop_pending(fold: Fold) -> list[CallId]:
    """The pending calls the agent's loop runs: every pending call but a host send."""
    return [c for c in fold.pending if c not in fold.host_calls]


def loop_parked(fold: Fold) -> list[ParkAddress]:
    """What the agent's turn waits on: every park but a host send's effect in doubt."""
    return [
        a
        for a in fold.parked
        if a.kind != "effect" or CallId(a.id.rpartition(":")[2]) not in fold.host_calls
    ]


def policy(fold: Fold) -> Policy | None:
    """The runtime policy pinned by thread_started; None means the ADR defaults apply."""
    started = fold.started
    return started.policy if started is not None and isinstance(started.policy, Policy) else None


def call_spec(fold: Fold, call_id: CallId) -> ToolSpec | None:
    """The spec a recorded call was made under; a later tools_changed never reclasses it. None:
    its tool was not in the set when the call was made."""
    return fold.call_specs.get(call_id)


def still_deferred(fold: Fold, spec: ToolSpec) -> bool:
    """Whether a spec is still deferred: flagged, and not loaded by a tools_loaded."""
    return spec.defer_loading is True and spec.name not in fold.loaded


def reject(
    event: Event | UnknownEvent, message: str, code: ErrorCode = "invalid_transition"
) -> ParseError:
    return ParseError(code, message, event.seq)
