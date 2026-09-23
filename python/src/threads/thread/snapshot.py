"""Capturing a fork point: the writer checks quiescence under its lease,
the snapshot's ledger row is written before the provider call, the image is proven to hold the
tree its manifest names, and the `snapshot` event is appended only then.

The proof is a restore of the image into a scratch sandbox, whose adapter verifies the
manifest (a manifest measured on the running parent can differ from what was
captured). The scratch sandbox is a provider resource like any other: its
own pending row before the restore, live after, released after; a crash in between is settled
by lookup or parks as unknown, and a failed close stays release_failed for gc."""

from dataclasses import dataclass, field
from typing import Final

from threads.log import ParseError, SnapshotData, SnapshotEvent
from threads.reduce.handlers import to_json
from threads.result import Err, Ok
from threads.sandbox.ledger import Fenced, Tracked, abandon, acquire, release_session
from threads.sandbox.protocol import Sandbox, SandboxError, SandboxSession, is_refusal
from threads.store import Draft, SqliteStore, Writer
from threads.store.worker import Clock

_PROOF: Final = object()


@dataclass(frozen=True, slots=True)
class VerifiedSnapshot:
    """A capture whose image a ledgered restore proved to hold its manifest. Only
    `verify_image` makes one; a provider's raw `SnapshotData` is a candidate, never a fork
    point."""

    data: SnapshotData
    proof: object = field(repr=False)

    def __post_init__(self) -> None:
        if self.proof is not _PROOF:
            raise TypeError("only verify_image proves a snapshot")


async def take_snapshot(  # noqa: PLR0913 - the corpus revision rides with the capture
    store: SqliteStore,
    writer: Writer,
    sandbox: Sandbox,
    session: SandboxSession,
    clock: Clock,
    *,
    knowledge_revision: int | None = None,
) -> Ok[SnapshotEvent] | Err[ParseError | SandboxError]:
    """Snapshots `session` at the writer's head. Refused (not_quiescent) while a turn is open,
    a call or model attempt is pending, anything is parked or an effect is unsettled, and when
    the captured image doesn't hold its manifest (the snapshot is then released)."""
    fold = writer.fold
    settled = all(status in ("committed", "resolved") for _, status in fold.effects.values())
    busy = fold.in_turn or fold.pending or fold.open_requests or fold.parked
    if busy or not settled:
        return Err(SandboxError("not_quiescent", "the branch is not at a quiescent boundary"))
    by = Fenced(store.ledger, writer.owner, store.context(writer.owner, clock), clock)
    how: Tracked[SnapshotData] = Tracked(
        "snapshot",
        lambda key: session.snapshot(key, by.context),
        lambda key: sandbox.lookup_snapshot(key, by.context),
        sandbox.info.lookup.snapshot,
        lambda data: data.snapshot_id,
    )
    captured = await acquire(store.ledger, writer.owner, sandbox.info.provider, how, clock)
    if isinstance(captured, Err):
        return captured
    row, data = captured.value
    verified = await verify_image(by, sandbox, data)
    if isinstance(verified, Err):
        return verified
    if verified.value is None:
        await abandon(by, sandbox, row)
        message = f"the image of {data.snapshot_id} doesn't hold manifest {data.manifest_hash}"
        return Err(SandboxError("not_quiescent", message))
    return await _append(writer, verified.value, knowledge_revision)


async def verify_image(
    by: Fenced, sandbox: Sandbox, data: SnapshotData
) -> Ok[VerifiedSnapshot | None] | Err[ParseError | SandboxError]:
    """The capture, proven, when a ledgered restore of the image verifies against its manifest
    hash; None when it doesn't. The scratch sandbox is released either way; an error is only a
    lost authority or a ledger failure."""
    context = by.context
    restore = Tracked(
        "sandbox",
        lambda key: sandbox.restore(data.snapshot_id, data.manifest_hash, key, context),
        lambda key: sandbox.lookup(key, context),
        sandbox.info.lookup.create,
        lambda s: s.id,
    )
    got = await acquire(by.ledger, by.owner, sandbox.info.provider, restore, by.clock)
    if isinstance(got, Err):
        error = got.error
        lost = isinstance(error, ParseError) or is_refusal(error)
        # Anything else (a mismatch, a failed or unresolvable restore) proves nothing.
        return got if lost else Ok(None)
    row, scratch = got.value
    released = await release_session(by, row, scratch)
    return released if isinstance(released, Err) else Ok(VerifiedSnapshot(data, _PROOF))


async def _append(
    writer: Writer, verified: VerifiedSnapshot, knowledge_revision: int | None
) -> Ok[SnapshotEvent] | Err[ParseError]:
    """Only a proven capture becomes a `snapshot` event, the one kind of fork point. With a
    knowledge binding it records the corpus revision a pinned fork searches as of."""
    wire = to_json(verified.data)
    if not isinstance(wire, dict):
        raise TypeError("snapshot data is an object")
    if knowledge_revision is not None:
        wire["knowledge_revision"] = knowledge_revision
    appended = await writer.append([Draft("snapshot", wire)])
    if isinstance(appended, Err):
        return appended
    (event,) = appended.value
    if not isinstance(event, SnapshotEvent):
        raise TypeError("a snapshot draft is stored as a snapshot event")
    return Ok(event)
