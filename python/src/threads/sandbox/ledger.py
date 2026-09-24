"""Provider calls that create or release a resource, recorded in the resource ledger. The `pending`
row and its operation key are durable before the create call. A lost
answer is resolved by that key; a key the adapter can't resolve parks as `unknown`
(resource_unknown). Nothing is ever created twice."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import assert_never

from threads.log import ParseError, SnapshotData
from threads.loop.model import (
    Found,
    LookupCapability,
    LookupUnknown,
    NotFound,
    NotFoundNonfinal,
)
from threads.result import Err, Ok
from threads.sandbox.protocol import (
    Looked,
    LooksUpSandbox,
    LooksUpSnapshot,
    Sandbox,
    SandboxContext,
    SandboxError,
    SandboxSession,
    is_refusal,
)
from threads.store import SqliteStore
from threads.store.lease import Owner
from threads.store.resources import Answer, Kind, Ledger, ReleaseOutcome, Resource
from threads.store.worker import Clock

_LOST = frozenset({"unavailable", "timeout"})
"""Answers that don't say whether the provider acted: resolved by the operation key."""

type Lookup[T] = Callable[[str], Awaitable[Looked[T]]]
"""A lookup by operation key, bound to its context."""


async def _unasked(_key: str) -> Ok[LookupUnknown]:
    return Ok(LookupUnknown("this sandbox has no lookup for it"))


def session_lookup(
    sandbox: Sandbox, context: SandboxContext
) -> tuple[Lookup[SandboxSession], LookupCapability]:
    """How a lost create or restore is found, and what that lookup can prove. A sandbox
    without the method is treated as declaring none; check() and the first run refuse one
    that declares a lookup, but a fork or gc outside a run doesn't pass through them."""
    if isinstance(sandbox, LooksUpSandbox):
        return (lambda key: sandbox.lookup(key, context)), sandbox.info.lookup.create
    return _unasked, "none"


def snapshot_lookup(
    sandbox: Sandbox, context: SandboxContext
) -> tuple[Lookup[SnapshotData], LookupCapability]:
    """How a lost capture is found, and what that lookup can prove (as `session_lookup`)."""
    if isinstance(sandbox, LooksUpSnapshot):
        return (lambda key: sandbox.lookup_snapshot(key, context)), sandbox.info.lookup.snapshot
    return _unasked, "none"


@dataclass(frozen=True, slots=True)
class Tracked[T]:
    """How to create one kind of resource and find it again by its operation key."""

    kind: Kind
    create: Callable[[str], Awaitable[Ok[T] | Err[SandboxError]]]
    lookup: Lookup[T]
    capability: LookupCapability
    ref: Callable[[T], str]


async def acquire[T](
    ledger: Ledger, owner: Owner, provider: str, how: Tracked[T], clock: Clock
) -> Ok[tuple[Resource, T]] | Err[SandboxError | ParseError]:
    """Writes the pending row, then calls the provider with its key. A typed failure proves
    nothing was created (released); a lost answer is looked up by the key."""
    pending = await ledger.pending(owner, provider, how.kind, clock())
    if isinstance(pending, Err):
        return pending
    row = pending.value
    made = await how.create(row.operation_key)
    answer, value = await _outcome(how, made, row.operation_key)
    ref = None if value is None else how.ref(value)
    # A stale owner leaves the row pending: cleanup resolves it by its key.
    settled = await ledger.resolve(owner, row, answer, clock(), ref)
    if isinstance(settled, Err):
        return settled
    if value is not None:
        return Ok((settled.value, value))
    if answer == "not_found" and isinstance(made, Err):
        return made
    message = f"{how.kind} {row.operation_key}: the create's outcome can't be established"
    return Err(SandboxError("resource_unknown", message))


async def _outcome[T](
    how: Tracked[T], made: Ok[T] | Err[SandboxError], key: str
) -> tuple[Answer, T | None]:
    if isinstance(made, Ok):
        return "found", made.value
    if made.error.code not in _LOST:
        return "not_found", None
    return await resolve_key(how.capability, how.lookup, key)


async def resolve_key[T](
    capability: LookupCapability,
    lookup: Lookup[T],
    key: str,
) -> tuple[Answer, T | None]:
    """What a lookup by operation key establishes. not_found counts only from a lookup whose
    declared capability is final; finality is never inferred from lookup being available. A
    refused fence proves nothing either: the ledger write that follows is fenced the same way,
    so the caller gets the refusal from it."""
    if capability == "none":
        return "unresolved", None
    looked = await lookup(key)
    if isinstance(looked, Err):
        return "unresolved", None
    result = looked.value
    match result:
        case Found(value=value):
            return "found", value
        case NotFound():
            return ("not_found" if capability == "final" else "unresolved"), None
        case NotFoundNonfinal() | LookupUnknown():
            return "unresolved", None
        case _:
            assert_never(result)


@dataclass(frozen=True, slots=True)
class Fenced:
    """An owner's ledger rows and provider calls, both fenced by its lease."""

    ledger: Ledger
    owner: Owner
    context: SandboxContext
    clock: Clock


async def release_session(
    by: Fenced, row: Resource, session: SandboxSession
) -> Ok[Resource] | Err[ParseError]:
    """The owner releases a sandbox it holds: releasing (fenced), then close."""
    releasing = await by.ledger.release(by.owner, row, by.clock())
    if isinstance(releasing, Err):
        return releasing
    closed = _closed(await session.close(by.context))
    return Ok(await _settled(by.ledger, releasing.value, closed, by.clock))


async def abandon(by: Fenced, sandbox: Sandbox, row: Resource) -> Resource:
    """The owner gives up a row it created: a pending one is resolved by its key, a live one
    is released. An unresolvable key stays unknown; a failed release is retried by gc. Only
    the adapter of the row's own provider may touch it."""
    if row.provider != sandbox.info.provider:
        return row
    if row.state == "pending":
        answer, ref = await _find(sandbox, row, by.context)
        resolved = await by.ledger.resolve(by.owner, row, answer, by.clock(), ref)
        row = resolved.value if isinstance(resolved, Ok) else row
    if row.state != "live" or row.ref is None:
        return row
    releasing = await by.ledger.release(by.owner, row, by.clock())
    if isinstance(releasing, Err):
        return row
    outcome = await _release_ref(sandbox, row.kind, row.ref, by.context)
    return await _settled(by.ledger, releasing.value, outcome, by.clock)


async def gc(store: SqliteStore, sandbox: Sandbox, clock: Clock) -> tuple[Resource, ...]:
    """Cleanup of the adapter's own provider's collectable rows, holding no branch lease and
    needing no owner branch: each row is claimed first, and a release is dispatched only while
    the row still carries that claim, so two gc runs never both dispatch. A failed release
    stays release_failed for the next run. Returns the tenant's rows afterwards."""
    ledger = store.ledger
    for row in await ledger.rows("releasing", "release_failed", "live"):
        claimed = (
            None if row.provider != sandbox.info.provider else await ledger.claim(row, clock())
        )
        if claimed is None:
            continue
        match claimed.state:
            case "release_failed":
                moved = await ledger.gc_retry(claimed, clock())
            case "live":
                moved = await ledger.gc_release(claimed, clock())
            case _:
                moved = claimed
        if moved is not None and moved.ref is not None:
            context = store.cleanup_context(moved, clock)
            outcome = await _release_ref(sandbox, moved.kind, moved.ref, context)
            await _settled(ledger, moved, outcome, clock)
    return await ledger.rows()


async def _find(
    sandbox: Sandbox, row: Resource, context: SandboxContext
) -> tuple[Answer, str | None]:
    """A pending row's answer by its operation key, and the ref when found."""
    key = row.operation_key
    if row.kind == "snapshot":
        lookup, capability = snapshot_lookup(sandbox, context)
        answer, snap = await resolve_key(capability, lookup, key)
        return answer, None if snap is None else snap.snapshot_id
    lookup, capability = session_lookup(sandbox, context)
    answer, session = await resolve_key(capability, lookup, key)
    return answer, None if session is None else session.id


async def _release_ref(
    sandbox: Sandbox, kind: Kind, ref: str, context: SandboxContext
) -> ReleaseOutcome | None:
    """What releasing `ref` established; None when the fence refused and nothing was asked."""
    if kind == "snapshot":
        released = await sandbox.release(ref, context)
        if isinstance(released, Ok):
            return "released" if released.value == "released" else "not_found"
        return None if is_refusal(released.error) else "release_error"
    return await _release_sandbox(sandbox, ref, context)


async def _release_sandbox(
    sandbox: Sandbox, ref: str, context: SandboxContext
) -> ReleaseOutcome | None:
    attached = await sandbox.attach(ref, context)
    if isinstance(attached, Ok):
        return _closed(await attached.value.close(context))
    match attached.error.code:
        case "stale_epoch" | "cleanup_claim_lost":
            return None
        case "not_found" if sandbox.info.lookup.create == "final":
            return "not_found"
        case "not_found" | "resource_unknown":
            return "unresolved"
        case _:
            return "release_error"


def _closed(closed: Ok[None] | Err[SandboxError]) -> ReleaseOutcome | None:
    # An error is never dropped: release_failed, retried by the next gc.
    if isinstance(closed, Ok):
        return "released"
    return None if is_refusal(closed.error) else "release_error"


async def _settled(
    ledger: Ledger, row: Resource, outcome: ReleaseOutcome | None, clock: Clock
) -> Resource:
    settled = None if outcome is None else await ledger.settle_release(row, outcome, clock())
    return row if settled is None else settled
