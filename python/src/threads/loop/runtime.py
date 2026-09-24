"""What one run of the loop is bound to, and how it stops.

The loop appends only through its branch's `Writer`, so every append is fenced by the lease
epoch: a stale owner's append fails before it can dispatch anything.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol

from pydantic import JsonValue

from threads.hooks.runner import HookRunner
from threads.log import (
    ArtifactRef,
    BranchId,
    Event,
    ModelRef,
    ParkAddress,
    ParkReason,
    ParseError,
    ToolCallData,
    ToolSpec,
)
from threads.loop import epoch
from threads.loop.covering import Covering
from threads.loop.history import CallState, open_cancel
from threads.loop.model import Model
from threads.loop.tools import ToolRunner
from threads.permissions import Decision
from threads.redaction import SecretInProviderOutputError, SecretInStoredBytesError
from threads.reduce import Fold
from threads.render.artifacts import read_verified
from threads.result import Err, Ok
from threads.store import Clock, Draft, SqliteStore, StoredEvent, Writer
from threads.store.companion import Companion
from threads.store.writer import Decide, DecideTx, Refusal

type Authorize = Callable[[Fold, ToolCallData, ToolSpec], Decision]
"""The permission fold for one call, against the current policy and mode."""

type Models = Callable[[ModelRef], Model | None]
"""The adapter serving a settings epoch's model; None: this run has none for it."""


def serving(*models: Model) -> Models:
    """The models a run was given, found by the (provider, name) their epoch names."""
    return lambda ref: next(
        (
            m
            for m in models
            if (m.info.model.provider, m.info.model.name) == (ref.provider, ref.name)
        ),
        None,
    )


type RunErrorCode = Literal[
    "model_unavailable",
    "context_exhausted",
    "max_output",
    "max_turns",
    "output_invalid",
    "input_denied",
    "stop_hook_limit",
    "model_error",
    "content_unsupported",
    "continuation_unsupported",
    "transport_fence_unsupported",
    "secret_in_provider_output",
    "secret_in_stored_bytes",
    "artifact_missing",
    "artifact_corrupt",
    "unmatched_external_op",
    "branch_busy",
    "branch_not_runnable",
]
"""spec/schema/host-api RunErrorCode."""

FAILED_CODES: Final[Mapping[str, RunErrorCode]] = {
    "error": "model_error",
    "interrupted": "model_error",
    "model_unavailable": "model_unavailable",
    "context_exhausted": "context_exhausted",
    "max_output": "max_output",
    "max_turns": "max_turns",
    "output_invalid": "output_invalid",
    "input_denied": "input_denied",
    "stop_hook_limit": "stop_hook_limit",
}
"""The turn_completed reasons that end a run failed, and their RunErrorCode."""

FAILED_MESSAGES: Final[Mapping[str, str]] = {
    "error": ("the model request failed; the thread's timeline has the provider's error"),
    "interrupted": (
        "the model's reply was cut off before it finished; run the thread again to continue"
    ),
    "model_unavailable": (
        "no model could be reached, including any fallbacks; check the provider's status "
        "and your API key"
    ),
    "context_exhausted": (
        "the conversation no longer fits the model's context window, even after "
        "compaction; start a new thread or use a model with a larger window"
    ),
    "max_output": (
        "the model hit its output token cap; raise the model's max output tokens or ask "
        "for a shorter answer"
    ),
    "max_turns": ("the run reached the agent's turn limit; raise max turns or split the task"),
    "output_invalid": (
        "the model's answer failed the output schema on every retry; raise output retries"
        " or loosen the schema"
    ),
    "input_denied": ("a hook or policy refused the input, so the model was not called"),
    "stop_hook_limit": (
        "a stop hook kept the run going past its limit; check when the hook asks to continue"
    ),
}
"""What went wrong and what to do next, per failed reason (the same text as TypeScript's
`MESSAGES`)."""


@dataclass(frozen=True, slots=True)
class Idle:
    """The turn ended; `reason` is its `turn_completed` reason."""

    reason: str


@dataclass(frozen=True, slots=True)
class Parked:
    reason: ParkReason
    pending: tuple[ParkAddress, ...]


@dataclass(frozen=True, slots=True)
class Failed:
    code: RunErrorCode
    message: str


type Halt = Idle | Parked | Failed


class Framework(Protocol):
    """The framework tools the agents layer runs on the log (spawn_agent, handoff, the team tools):
    each advances its call to a result, or halts."""

    @property
    def names(self) -> frozenset[str]: ...

    async def run(self, rt: "Runtime", state: CallState) -> "Halt | None": ...

    async def flush(self, rt: "Runtime") -> "Halt | None":
        """Records what finished in the background (a child's result) at a step boundary."""
        ...

    async def deliver(self, rt: "Runtime") -> "Halt | bool":
        """Before a turn request: delivers pending team messages; True when it appended."""
        ...


@dataclass(frozen=True, slots=True)
class Barred:
    """A batch the cancel barrier changed: `kept` is what was appended (maybe nothing). The
    cancellation step closes the turn next."""

    kept: tuple[StoredEvent, ...]


type Appended = Ok[tuple[StoredEvent, ...]] | Err[ParseError] | Barred
"""What a loop append did: every draft appended, a refusal, or a batch the barrier changed."""


@dataclass(frozen=True, slots=True)
class Runtime:
    store: SqliteStore
    writer: Writer
    models: Models
    """Every model this thread may use, the primary and its fallbacks: each request goes to
    the current settings epoch's."""
    tools: ToolRunner
    authorize: Authorize
    clock: Clock
    wait_until: Callable[[int], Awaitable[None]]
    """Waits until the given epoch ms: a real sleep, or a test clock advanced at once."""
    output: Callable[[JsonValue], str | None] | None = None
    """The structured-output binding: why a final_output candidate fails, or None. Absent:
    every candidate is rejected (fail closed)."""
    observe: Callable[[Sequence[StoredEvent]], None] = lambda _events: None
    """Receives each committed batch: the stream's subscription to the log."""
    hooks: HookRunner = field(default_factory=HookRunner)
    """The run's extension hooks; none by default."""
    read_file: Callable[[str], Awaitable[bytes | None]] | None = None
    """L3 restore's sandbox read, a framework read_only operation; None
    when there is no sandbox or the file can't be read."""
    budgets: tuple[Covering, ...] = ()
    """Ancestors' budgets that also cover this thread."""
    framework: Framework | None = None
    """Subagents, handoffs and teams, bound by the agents layer; None: those tools aren't run."""
    concurrent: frozenset[str] = frozenset()
    """The bound tools declared `concurrent=True` (pinned by config_hash): their read-only calls
    of one response may run in a group (threads.loop.parallel). Never a built-in or MCP tool."""

    @property
    def fold(self) -> Fold:
        return self.writer.fold

    @property
    def events(self) -> Sequence[Event]:
        return self.writer.fold.events

    async def append(self, *drafts: Draft) -> Appended:
        """Appends one durable batch. Nothing is dispatched on its account until this returns."""
        return await self.append_with(drafts, None)

    async def append_built(self, build: Callable[[Fold], Sequence[Draft]]) -> Appended:
        """`append`, with the batch built from the committed fold under the writer's lock, so a
        decision that reads the log (a background wake) sees exactly what the append extends."""
        return await self.append_with((), None, build)

    async def append_with(
        self,
        drafts: Sequence[Draft],
        companion: Companion | None,
        build: Callable[[Fold], Sequence[Draft]] | None = None,
    ) -> Appended:
        """`append`, with host rows bound to the same transaction. Every loop append passes the
        cancel barrier (`after_barrier`) under the writer's lock: a batch it changed comes back
        as `Barred`, never as a plain success, so a caller that opened work (or ended the
        turn) knows it didn't."""
        changed: list[bool] = []

        def admit(fold: Fold, batch: Sequence[Draft]) -> Sequence[Draft]:
            wanted = batch if build is None else build(fold)
            kept = after_barrier(fold, wanted)
            changed.append(len(kept) != len(wanted))
            return kept

        done = await self.writer.append(drafts, companion, admit=admit)
        if isinstance(done, Err):
            return done
        self.observe(done.value)
        return Barred(done.value) if any(changed) else done

    async def append_decided[E](self, decide: Decide[E]) -> Appended | Refusal[E]:
        """A decided append (a team operation): `decide` reads the store inside the append's
        transaction and builds the batch, which passes the cancel barrier as `append_with`'s
        does, or refuses. A refusal appends nothing and comes back for the caller to record."""
        changed: list[bool] = []

        def barred(tx: DecideTx) -> Sequence[Draft] | Refusal[E]:
            decided = decide(tx)
            if isinstance(decided, Refusal):
                return decided
            kept = after_barrier(tx.fold, decided)
            changed.append(len(kept) != len(decided))
            return kept

        done = await self.writer.append_decided(barred)
        if isinstance(done, Refusal | Err):
            return done
        self.observe(done.value)
        return Barred(done.value) if any(changed) else done


OPENS_WORK: Final = frozenset(
    {
        "model_request",
        "effect_begin",
        "handoff",
        "agent_spawned",
        "retry_scheduled",
        "settings_changed",
    }
)
"""Events that start new work: a request, a dispatch, a handoff target, a child, a retry wait,
a model switch. Teams' member starts and deliveries join this list when they become writable
(spec/schema/README.md, Teams); TypeScript's `OPENS_WORK` (loop/turn.ts) is the same list."""


def after_barrier(fold: Fold, drafts: Sequence[Draft]) -> Sequence[Draft]:
    """Nothing new after a cancel barrier (spec/schema/README.md). With a cancel pending in the
    open turn, a batch that starts new work is refused (only the hook decisions that ran for it
    are kept, as the audit record), and any other batch keeps what it owes (an abandonment, a
    side request's compaction_failed) but no turn_completed other than cancelled. The
    cancellation step closes the turn."""
    if not fold.in_turn or open_cancel(fold.events) is None:
        return drafts
    if any(d.type in OPENS_WORK for d in drafts):
        return [d for d in drafts if d.type == "hook_decision"]
    return [d for d in drafts if d.type != "turn_completed" or d.data.get("reason") == "cancelled"]


@dataclass(frozen=True, slots=True)
class WriterContext:
    """spec/api.json `ModelContext`, bound to the writer that made it: its fence, reads and puts
    never speak for another branch or epoch."""

    rt: Runtime

    @property
    def branch_id(self) -> BranchId:
        return self.rt.writer.branch_id

    @property
    def epoch(self) -> int:
        return self.rt.writer.epoch

    async def fence(self) -> Ok[None] | Err[ParseError]:
        return await self.rt.writer.fence()

    async def read(self, ref: ArtifactRef) -> Ok[bytes] | Err[ParseError]:
        got = await self.rt.store.get_artifact(ref.sha256)
        return read_verified(lambda _sha: got, ref, None)

    async def put(self, data: bytes, media_type: str) -> ArtifactRef:
        # Replayed byte-exact, so never edited: a secret in it ends the turn instead (C5).
        try:
            sha = await self.rt.store.put_artifact(data)
        except SecretInStoredBytesError:
            raise SecretInProviderOutputError from None
        return ArtifactRef(sha256=sha, bytes=len(data), media_type=media_type)


def epoch_model(rt: Runtime) -> Model | None:
    """The adapter for the current settings epoch."""
    return rt.models(epoch.current(rt.fold).model)


async def fence(rt: Runtime) -> Failed | None:
    """Re-checks the lease right before a dispatch; None means this owner may still send."""
    held = await rt.writer.fence()
    return lost(held.error) if isinstance(held, Err) else None


LOST: Final = frozenset({"stale_epoch", "seq_conflict", "writer_poisoned"})
"""Append failures that mean this run no longer owns the branch."""


def lost(error: ParseError) -> Failed:
    """An append that failed: a lost lease or moved head is branch_busy; stored bytes that would
    hold a registered value end the run with that code (C5); anything else is a bug in what the
    loop wrote, which validate_next refused."""
    if error.code in LOST:
        return Failed("branch_busy", error.message)
    if error.code == "secret_in_stored_bytes":
        return Failed("secret_in_stored_bytes", error.message)
    raise AssertionError(f"the loop wrote an invalid event: {error.code}: {error.message}")
