"""What one run of the loop is bound to, and how it stops.

The loop appends only through its branch's `Writer`, so every append is fenced by the lease
epoch: a stale owner's append fails before it can dispatch anything.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from threads.log import Event, ParkAddress, ParseError, ToolCallData, ToolSpec
from threads.loop.model import Model
from threads.loop.tools import ToolRunner
from threads.permissions import Decision
from threads.reduce import Fold
from threads.result import Err, Ok
from threads.store import Clock, Draft, SqliteStore, StoredEvent, Writer

type Authorize = Callable[[Fold, ToolCallData, ToolSpec], Decision]
"""The permission fold for one call, against the current policy and mode."""

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
    "artifact_missing",
    "artifact_corrupt",
    "unmatched_external_op",
    "branch_busy",
    "branch_not_runnable",
]
"""spec/schema/host-api RunErrorCode."""


@dataclass(frozen=True, slots=True)
class Idle:
    """The turn ended; `reason` is its `turn_completed` reason."""

    reason: str


@dataclass(frozen=True, slots=True)
class Parked:
    reason: Literal["awaiting_approval", "effect_unknown", "awaiting_input", "awaiting_resource"]
    pending: tuple[ParkAddress, ...]


@dataclass(frozen=True, slots=True)
class Failed:
    code: RunErrorCode
    message: str


type Halt = Idle | Parked | Failed


@dataclass(frozen=True, slots=True)
class Runtime:
    store: SqliteStore
    writer: Writer
    model: Model
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

    @property
    def fold(self) -> Fold:
        return self.writer.fold

    @property
    def events(self) -> Sequence[Event]:
        return self.writer.fold.events

    async def append(self, *drafts: Draft) -> Ok[tuple[StoredEvent, ...]] | Err[ParseError]:
        """Appends one durable batch. Nothing is dispatched on its account until this returns."""
        done = await self.writer.append(drafts)
        if isinstance(done, Ok):
            self.observe(done.value)
        return done


def lost(error: ParseError) -> Failed:
    """An append that failed: a lost lease or moved head is branch_busy; anything else is a
    bug in what the loop wrote, which validate_next refused."""
    if error.code in ("stale_epoch", "seq_conflict", "writer_poisoned"):
        return Failed("branch_busy", error.message)
    raise AssertionError(f"the loop wrote an invalid event: {error.code}: {error.message}")
