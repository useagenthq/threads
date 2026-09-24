"""The fork operation: a new branch of the same thread at an eligible
snapshot, restored into an isolated sandbox that the child owns.

Only a quiescent, unexpired snapshot event is a fork point. The child is `forking` (unlisted)
until its `fork` event is written; the restore's ledger row is written before the provider
call. On any failure the child becomes `fork_failed` and what the fork created is released,
except a restore whose outcome the adapter can't establish: that row stays `unknown` for an
operator (resource_unknown). The parent is never written.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from threads.log import (
    BranchId,
    Event,
    EventId,
    ForkEvent,
    ParseError,
    SnapshotEvent,
)
from threads.reduce import Fold
from threads.result import Err, Ok
from threads.sandbox.ledger import Fenced, Tracked, abandon, acquire, release_session
from threads.sandbox.protocol import Sandbox, SandboxError, session_lookup
from threads.store import SqliteStore, Writer
from threads.store.worker import Clock

if TYPE_CHECKING:
    from pydantic import JsonValue

type KnowledgePolicy = Literal["pinned", "current"]


@dataclass(frozen=True, slots=True)
class ForkAt:
    parent: BranchId
    """The branch whose resolved chain holds the snapshot."""
    point: EventId
    """The snapshot event."""
    child: BranchId
    knowledge: KnowledgePolicy = "pinned"


async def fork_branch(
    store: SqliteStore, sandbox: Sandbox | None, at: ForkAt, holder_id: str, clock: Clock
) -> Ok[Writer] | Err[ParseError]:
    """Forks at `at.point` and returns the child's writer, holding its first lease."""
    eligible = await _restorable(store, sandbox, at, clock)
    if isinstance(eligible, Err):
        return eligible
    snap, sandbox = eligible.value
    begun = await store.begin_fork(snap.branch_id, snap.seq, at.child, holder_id, clock)
    if isinstance(begun, Err):
        return begun
    forking = begun.value
    context = store.context(forking.owner, clock)
    restore = Tracked(
        "sandbox",
        lambda key: sandbox.restore(snap.data.snapshot_id, snap.data.manifest_hash, key, context),
        *session_lookup(sandbox, context),
        lambda session: session.id,
    )
    restored = await acquire(store.ledger, forking.owner, snap.data.provider, restore, clock)
    if isinstance(restored, Err):
        await store.fail_fork(forking.row.branch_id)
        return Err(_failure(restored.error, snap.seq))
    row, session = restored.value
    data: dict[str, JsonValue] = {
        "reason": "snapshot",
        "sandbox_id": session.id,
        "knowledge_policy": at.knowledge,
    }
    finished = await store.finish_fork(forking, data, clock)
    if isinstance(finished, Ok) and finished.value is not None:
        return Ok(finished.value)
    await release_session(Fenced(store.ledger, forking.owner, context, clock), row, session)
    await store.fail_fork(forking.row.branch_id)
    return finished if isinstance(finished, Err) else Err(_not_runnable(snap.seq))


async def recover_forks(
    store: SqliteStore, sandbox: Sandbox, holder_id: str, clock: Clock
) -> tuple[BranchId, ...]:
    """A fork is never resumed (spec/schema/README.md): each fork a crash left `forking` gives
    up every ledger row it wrote (released, or unknown for an operator) and becomes
    `fork_failed`. Returns the children it failed."""
    failed: list[BranchId] = []
    for owner in await store.interrupted_forks(holder_id, clock):
        by = Fenced(store.ledger, owner, store.context(owner, clock), clock)
        for row in await store.ledger.rows():
            if row.owner_branch_id == owner.branch_id:
                await abandon(by, sandbox, row)
        await store.fail_fork(owner.branch_id)
        failed.append(owner.branch_id)
    return tuple(failed)


async def _restorable(
    store: SqliteStore, sandbox: Sandbox | None, at: ForkAt, clock: Clock
) -> Ok[tuple[SnapshotEvent, Sandbox]] | Err[ParseError]:
    read = await store.read(at.parent, clock())
    if isinstance(read, Err):
        return read
    eligible = fork_point(read.value.fold, at.point)
    if isinstance(eligible, Err):
        return eligible
    snap = eligible.value
    if sandbox is None or sandbox.info.provider != snap.data.provider:
        message = f"no sandbox adapter for provider {snap.data.provider}"
        return Err(ParseError("sandbox_required", message, snap.seq))
    return Ok((snap, sandbox))


def fork_point(fold: Fold, point: EventId) -> Ok[SnapshotEvent] | Err[ParseError]:
    """The snapshot event `point` if it is an eligible fork point on this chain now."""
    event = next((e for e in fold.events if e.event_id == point), None)
    if not isinstance(event, SnapshotEvent):
        seq = None if event is None else event.seq
        return Err(ParseError("no_snapshot_boundary", f"{point} is not a snapshot event", seq))
    expires = event.data.expires_at
    if expires is not None and expires <= fold.now:
        return Err(ParseError("snapshot_expired", f"snapshot {point} has expired", event.seq))
    if (event.seq, event.event_id) not in fold.fork_points:
        message = f"snapshot {point} was not taken at a quiescent boundary"
        return Err(ParseError("no_snapshot_boundary", message, event.seq))
    return Ok(event)


def knowledge_revision(events: tuple[Event, ...] | list[Event]) -> int | None:
    """The knowledge revision a branch searches as of: its fork snapshot's
    under `pinned`, None (the live corpus) under `current`, for a root, or for a snapshot
    taken without a knowledge binding."""
    forks = [e for e in events if isinstance(e, ForkEvent)]
    if not forks or forks[-1].data.knowledge_policy != "pinned":
        return None
    at_seq = forks[-1].seq - 1
    snap = next((e for e in events if e.seq == at_seq and isinstance(e, SnapshotEvent)), None)
    revision = None if snap is None else snap.data.knowledge_revision
    return revision if isinstance(revision, int) else None


def _failure(error: SandboxError | ParseError, seq: int) -> ParseError:
    if isinstance(error, ParseError):
        return error
    match error.code:
        case (
            "snapshot_expired" | "snapshot_missing" | "snapshot_manifest_mismatch"
        ) | "resource_unknown" as code:
            return ParseError(code, error.message, seq)
        case _:
            return ParseError("snapshot_restore_failed", error.message, seq)


def _not_runnable(seq: int) -> ParseError:
    # Only a repair child is not runnable, and a snapshot fork never is one.
    return ParseError("invalid_transition", "the forked child is not runnable", seq)
