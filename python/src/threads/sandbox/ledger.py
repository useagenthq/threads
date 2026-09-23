"""Provider calls that create or release a resource, recorded in the resource ledger (). The `pending` row and its operation key are durable before the create call. A lost
answer is resolved by that key; a key the adapter can't resolve parks as `unknown`
(resource_unknown). Nothing is ever created twice."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import assert_never

from threads.log import ParseError
from threads.loop.model import (
    Found,
    LookupCapability,
    LookupResult,
    LookupUnknown,
    NotFound,
    NotFoundNonfinal,
)
from threads.result import Err, Ok
from threads.sandbox.protocol import Sandbox, SandboxError, SandboxSession
from threads.store.lease import Owner
from threads.store.resources import Answer, Kind, Ledger, ReleaseOutcome, Resource
from threads.store.worker import Clock

_LOST = frozenset({"unavailable", "timeout"})
"""Answers that don't say whether the provider acted: resolved by the operation key."""


@dataclass(frozen=True, slots=True)
class Tracked[T]:
    """How to create one kind of resource and find it again by its operation key."""

    kind: Kind
    create: Callable[[str], Awaitable[Ok[T] | Err[SandboxError]]]
    lookup: Callable[[str], Awaitable[LookupResult[T]]]
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
    lookup: Callable[[str], Awaitable[LookupResult[T]]],
    key: str,
) -> tuple[Answer, T | None]:
    """What a lookup by operation key establishes. not_found counts only from a lookup whose
    declared capability is final; finality is never inferred from lookup being available."""
    if capability == "none":
        return "unresolved", None
    result = await lookup(key)
    match result:
        case Found(value=value):
            return "found", value
        case NotFound():
            return ("not_found" if capability == "final" else "unresolved"), None
        case NotFoundNonfinal() | LookupUnknown():
            return "unresolved", None
        case _:
            assert_never(result)


async def release_session(
    ledger: Ledger, owner: Owner, row: Resource, session: SandboxSession, clock: Clock
) -> Ok[Resource] | Err[ParseError]:
    """The owner releases a sandbox it holds: releasing (fenced), then close."""
    releasing = await ledger.release(owner, row, clock())
    if isinstance(releasing, Err):
        return releasing
    closed = await session.close()
    return Ok(await _settled(ledger, releasing.value, _closed(closed), clock))


async def abandon(
    ledger: Ledger, owner: Owner, sandbox: Sandbox, row: Resource, clock: Clock
) -> Resource:
    """The owner gives up a row it created: a pending one is resolved by its key, a live one
    is released. An unresolvable key stays unknown; a failed release is retried by gc."""
    if row.state == "pending":
        answer, ref = await _find(sandbox, row)
        resolved = await ledger.resolve(owner, row, answer, clock(), ref)
        row = resolved.value if isinstance(resolved, Ok) else row
    if row.state != "live" or row.ref is None:
        return row
    releasing = await ledger.release(owner, row, clock())
    if isinstance(releasing, Err):
        return row
    outcome = await _release_ref(sandbox, row.kind, row.ref)
    return await _settled(ledger, releasing.value, outcome, clock)


async def gc(ledger: Ledger, sandbox: Sandbox, clock: Clock) -> tuple[Resource, ...]:
    """Cleanup of one provider's rows, holding no branch lease: resolves pending rows whose
    creator crashed, retries failed releases, and finishes releases in progress. Returns the
    tenant's rows afterwards."""
    provider = sandbox.info.provider
    for row in await ledger.orphaned(clock()):
        if row.provider == provider:
            answer, ref = await _find(sandbox, row)
            await ledger.gc_resolve(row, answer, clock(), ref)
    for row in await ledger.rows("release_failed"):
        if row.provider == provider:
            await ledger.gc_retry(row, clock())
    for row in await ledger.rows("releasing"):
        if row.provider == provider and row.ref is not None:
            await _settled(ledger, row, await _release_ref(sandbox, row.kind, row.ref), clock)
    return await ledger.rows()


async def _find(sandbox: Sandbox, row: Resource) -> tuple[Answer, str | None]:
    """A pending row's answer by its operation key, and the ref when found."""
    info = sandbox.info.lookup
    if row.kind == "snapshot":
        answer, snap = await resolve_key(info.snapshot, sandbox.lookup_snapshot, row.operation_key)
        return answer, None if snap is None else snap.snapshot_id
    answer, session = await resolve_key(info.create, sandbox.lookup, row.operation_key)
    return answer, None if session is None else session.id


async def _release_ref(sandbox: Sandbox, kind: Kind, ref: str) -> ReleaseOutcome:
    if kind == "snapshot":
        released = await sandbox.release(ref)
        if isinstance(released, Err):
            return "release_error"
        return "released" if released.value == "released" else "not_found"
    attached = await sandbox.attach(ref)
    if isinstance(attached, Ok):
        return _closed(await attached.value.close())
    match attached.error.code:
        case "not_found" if sandbox.info.lookup.create == "final":
            return "not_found"
        case "not_found" | "resource_unknown":
            return "unresolved"
        case _:
            return "release_error"


def _closed(closed: Ok[None] | Err[SandboxError]) -> ReleaseOutcome:
    # An error is never dropped: release_failed, retried by the next gc.
    return "released" if isinstance(closed, Ok) else "release_error"


async def _settled(
    ledger: Ledger, row: Resource, outcome: ReleaseOutcome, clock: Clock
) -> Resource:
    settled = await ledger.settle_release(row, outcome, clock())
    return row if settled is None else settled
