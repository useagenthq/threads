"""Capturing a fork point: the writer checks quiescence under its lease,
the snapshot's ledger row is written before the provider call, and the `snapshot` event is
appended only once the provider reports the capture durable."""

from threads.log import ParseError, SnapshotData, SnapshotEvent
from threads.reduce.handlers import to_json
from threads.result import Err, Ok
from threads.sandbox.ledger import Tracked, acquire
from threads.sandbox.protocol import Sandbox, SandboxError, SandboxSession
from threads.store import Draft, SqliteStore, Writer
from threads.store.worker import Clock


async def take_snapshot(
    store: SqliteStore, writer: Writer, sandbox: Sandbox, session: SandboxSession, clock: Clock
) -> Ok[SnapshotEvent] | Err[ParseError | SandboxError]:
    """Snapshots `session` at the writer's head. Refused (not_quiescent) while a turn is open,
    a call or model attempt is pending, anything is parked or an effect is unsettled."""
    fold = writer.fold
    settled = all(status in ("committed", "resolved") for _, status in fold.effects.values())
    busy = fold.in_turn or fold.pending or fold.open_requests or fold.parked
    if busy or not settled:
        return Err(SandboxError("not_quiescent", "the branch is not at a quiescent boundary"))
    context = store.context(writer.owner, clock)
    how: Tracked[SnapshotData] = Tracked(
        "snapshot",
        lambda key: session.snapshot(key, context),
        lambda key: sandbox.lookup_snapshot(key, context),
        sandbox.info.lookup.snapshot,
        lambda data: data.snapshot_id,
    )
    captured = await acquire(store.ledger, writer.owner, sandbox.info.provider, how, clock)
    if isinstance(captured, Err):
        return captured
    data = to_json(captured.value[1])
    if not isinstance(data, dict):
        raise TypeError("snapshot data is an object")
    appended = await writer.append([Draft("snapshot", data)])
    if isinstance(appended, Err):
        return appended
    (event,) = appended.value
    if not isinstance(event, SnapshotEvent):
        raise TypeError("a snapshot draft is stored as a snapshot event")
    return Ok(event)
