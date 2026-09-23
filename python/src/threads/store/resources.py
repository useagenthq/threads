"""The resource ledger: provider resources a branch created, and their cleanup.

A `pending` row with a fresh `operation_key` is durable before the provider call that creates
the resource, so a lost answer or a crash resolves by that key and nothing is created twice.
Every row belongs to one tenant, and no statement reaches another tenant's rows.

Each path may move a row only from the states it names, along `MOVES`:
- the owner, fenced by its lease: `pending`, `resolve` (a pending row), `release` (a live one);
- the provider's answer to a release: `settle_release` (a releasing row);
- cleanup (gc), which holds no branch lease and outlives deleted branches: it first `claim`s a
  collectable row (releasing, release_failed, or live past its provider expiry) by
  compare-and-set, then `gc_retry` (release_failed) or `gc_release` (expired live) move it only
  while it still carries that claim. A later claim supersedes an earlier one, so of two gc
  runs only the latest dispatches (spec/api.json `SandboxAuthority`).
A pending row a crash left behind is resolved by its owner once it takes the lease again.
"""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from threads.log import BranchId, ParseError
from threads.result import Err, Ok
from threads.store import lease
from threads.store.lines import uuid7
from threads.store.sql import branch, int_of, text_of, transaction
from threads.store.worker import Worker

type Kind = Literal["sandbox", "snapshot"]
type State = Literal["pending", "live", "releasing", "released", "release_failed", "unknown"]
type Answer = Literal["found", "not_found", "unresolved"]
"""What a create (or its lookup) established: found (it exists; its ref is known), not_found
(final: nothing was created), unresolved (no final answer: the row parks for an operator)."""
type ReleaseOutcome = Literal["released", "not_found", "release_error", "unresolved"]
"""What a release established: released, not_found (final: already gone), release_error (it
failed; gc retries), unresolved (no answer about the resource at all)."""
type Move = Answer | ReleaseOutcome | Literal["release", "retry"]

MOVES: Final[Mapping[tuple[State, Move], State]] = {
    ("pending", "found"): "live",
    ("pending", "not_found"): "released",
    ("pending", "unresolved"): "unknown",
    # An operator records the ref, or confirms nothing exists.
    ("unknown", "found"): "live",
    ("unknown", "not_found"): "released",
    ("live", "release"): "releasing",
    ("releasing", "released"): "released",
    ("releasing", "not_found"): "released",
    ("releasing", "unresolved"): "unknown",
    ("releasing", "release_error"): "release_failed",
    ("release_failed", "retry"): "releasing",
}
"""Every legal move. released is final; nothing ever returns to pending."""


def next_state(state: State, move: Move) -> State | None:
    return MOVES.get((state, move))


@dataclass(frozen=True, slots=True)
class Resource:
    resource_id: str
    owner_branch_id: BranchId
    provider: str
    kind: Kind
    ref: str | None
    state: State
    operation_key: str
    acquired_at: int
    expires_at: int | None
    released_at: int | None
    release_outcome: str | None
    """The move that released it: released, or not_found (never created, or already gone)."""
    cleanup_claim: str | None = None
    """The latest gc claim on the row, if any."""


_COLUMNS = (
    "resource_id, owner_branch_id, provider, kind, ref, state, operation_key, acquired_at,"
    " expires_at, released_at, release_outcome, cleanup_claim"
)
_COLLECTABLE = (
    "(state IN ('releasing', 'release_failed')"
    " OR (state = 'live' AND expires_at IS NOT NULL AND expires_at <= ?))"
)


class Ledger:
    """The ledger rows of one tenant, on the store's thread."""

    def __init__(self, worker: Worker, tenant_id: str) -> None:
        self._worker = worker
        self._tenant = tenant_id

    async def pending(
        self, owner: lease.Owner, provider: str, kind: Kind, now: int
    ) -> Ok[Resource] | Err[ParseError]:
        """Writes a `pending` row with a fresh operation key, before the provider call. A stale
        owner is refused (stale_epoch) and must not call the provider."""
        key = uuid7(now)
        row = Resource(
            key, owner.branch_id, provider, kind, None, "pending", key, now, None, None, None
        )
        tenant = self._tenant

        def insert(conn: sqlite3.Connection) -> Resource | ParseError:
            with transaction(conn):
                refused = _fence(conn, tenant, owner, now)
                if refused is None:
                    _insert(conn, tenant, row)
            return refused or row

        return _result(await self._worker.call(insert))

    async def resolve(
        self, owner: lease.Owner, row: Resource, answer: Answer, now: int, ref: str | None = None
    ) -> Ok[Resource] | Err[ParseError]:
        """Records what the owner's provider call (or its lookup by key) established about its
        pending row. `found` records `ref`."""
        return await self._owned(owner, row, _Step("pending", answer, now, ref))

    async def release(
        self, owner: lease.Owner, row: Resource, now: int
    ) -> Ok[Resource] | Err[ParseError]:
        """Moves the owner's live row to `releasing` before the provider is asked to release
        it. A stale owner is refused (stale_epoch)."""
        return await self._owned(owner, row, _Step("live", "release", now))

    async def settle_release(
        self, row: Resource, outcome: ReleaseOutcome, now: int
    ) -> Resource | None:
        """Records the provider's answer to a release of a `releasing` row; None when the row
        is not this tenant's or not releasing."""
        return await self._moved(row, _Step("releasing", outcome, now))

    async def claim(self, row: Resource, now: int) -> Resource | None:
        """gc claims a collectable row with a fresh token, superseding any earlier claim; None
        when the row is not this tenant's or not collectable."""
        tenant, token = self._tenant, uuid7(now)

        def put(conn: sqlite3.Connection) -> Resource | None:
            with transaction(conn):
                conn.execute(
                    "UPDATE resources SET cleanup_claim = ? WHERE tenant_id = ?"  # noqa: S608 - fixed text
                    f" AND resource_id = ? AND {_COLLECTABLE}",
                    (token, tenant, row.resource_id, now),
                )
                claimed = _select(conn, tenant, row.resource_id)
            return claimed if claimed is not None and claimed.cleanup_claim == token else None

        return await self._worker.call(put)

    async def holds(self, resource_id: str, claim: str, now: int) -> bool:
        """The cleanup fence: the row still carries `claim` and is still collectable."""
        tenant = self._tenant

        def check(conn: sqlite3.Connection) -> bool:
            found: tuple[object] | None = conn.execute(
                "SELECT 1 FROM resources WHERE tenant_id = ? AND resource_id = ?"  # noqa: S608 - fixed text
                f" AND cleanup_claim = ? AND {_COLLECTABLE}",
                (tenant, resource_id, claim, now),
            ).fetchone()
            return found is not None

        return await self._worker.call(check)

    async def gc_retry(self, row: Resource, now: int) -> Resource | None:
        """Moves a release_failed row back to releasing, only under its current claim."""
        return await self._claimed(row, _Step("release_failed", "retry", now))

    async def gc_release(self, row: Resource, now: int) -> Resource | None:
        """Moves a live row past its provider expiry to releasing, only under its current
        claim: its owner may be gone, and the provider already let it die."""
        return await self._claimed(row, _Step("live", "release", now))

    async def rows(self, *states: State) -> tuple[Resource, ...]:
        """This tenant's rows in the given states (all when none), oldest first."""
        tenant = self._tenant

        def select(conn: sqlite3.Connection) -> tuple[Resource, ...]:
            found: list[tuple[object, ...]] = conn.execute(
                f"SELECT {_COLUMNS} FROM resources WHERE tenant_id = ? ORDER BY rowid",  # noqa: S608 - fixed columns
                (tenant,),
            ).fetchall()
            parsed = (_row(values) for values in found)
            return tuple(r for r in parsed if not states or r.state in states)

        return await self._worker.call(select)

    async def _owned(
        self, owner: lease.Owner, row: Resource, step: "_Step"
    ) -> Ok[Resource] | Err[ParseError]:
        tenant = self._tenant

        def apply(conn: sqlite3.Connection) -> Resource | ParseError:
            with transaction(conn):
                refused = _fence(conn, tenant, owner, step.now)
                if refused is None and row.owner_branch_id != owner.branch_id:
                    refused = ParseError("stale_epoch", f"{row.resource_id} has another owner")
                moved = None if refused else _move(conn, tenant, row.resource_id, step)
            if refused is not None:
                return refused
            message = f"{row.resource_id} is not {step.source}, or can't move by {step.move}"
            return moved or ParseError("invalid_transition", message)

        return _result(await self._worker.call(apply))

    async def _moved(self, row: Resource, step: "_Step") -> Resource | None:
        tenant = self._tenant

        def apply(conn: sqlite3.Connection) -> Resource | None:
            with transaction(conn):
                return _move(conn, tenant, row.resource_id, step)

        return await self._worker.call(apply)

    async def _claimed(self, row: Resource, step: "_Step") -> Resource | None:
        tenant, claim = self._tenant, row.cleanup_claim

        def apply(conn: sqlite3.Connection) -> Resource | None:
            with transaction(conn):
                current = _select(conn, tenant, row.resource_id)
                if claim is None or current is None or current.cleanup_claim != claim:
                    return None
                expired = current.expires_at is not None and current.expires_at <= step.now
                if current.state == "live" and not expired:
                    return None
                return _move(conn, tenant, row.resource_id, step)

        return await self._worker.call(apply)


@dataclass(frozen=True, slots=True)
class _Step:
    """One move of one row, legal only from `source`."""

    source: State
    move: Move
    now: int
    ref: str | None = None
    """Recorded by `found`."""


def _fence(
    conn: sqlite3.Connection, tenant_id: str, owner: lease.Owner, now: int
) -> ParseError | None:
    """The owner is this tenant's branch and its lease is still live at its epoch."""
    found = branch(conn, owner.branch_id)
    if found is None or found.tenant_id != tenant_id:
        return ParseError("branch_not_found", f"no branch {owner.branch_id}")
    return lease.check(conn, owner.branch_id, owner.lease, now)


def _insert(conn: sqlite3.Connection, tenant_id: str, row: Resource) -> None:
    conn.execute(
        f"INSERT INTO resources (tenant_id, {_COLUMNS}) VALUES ({', '.join('?' * 13)})",  # noqa: S608 - fixed columns
        (
            tenant_id,
            row.resource_id,
            row.owner_branch_id,
            row.provider,
            row.kind,
            row.ref,
            row.state,
            row.operation_key,
            row.acquired_at,
            row.expires_at,
            row.released_at,
            row.release_outcome,
            row.cleanup_claim,
        ),
    )


def _move(
    conn: sqlite3.Connection, tenant_id: str, resource_id: str, step: _Step
) -> Resource | None:
    """Moves this tenant's row by `step`; None if the row is not in the step's source state
    now, or the move is not legal from there."""
    current = _select(conn, tenant_id, resource_id)
    to = None if current is None else next_state(current.state, step.move)
    if current is None or current.state != step.source or to is None:
        return None
    released = (
        (step.now, step.move)
        if to == "released"
        else (current.released_at, current.release_outcome)
    )
    ref = step.ref
    conn.execute(
        "UPDATE resources SET state = ?, ref = ?, released_at = ?, release_outcome = ?"
        " WHERE tenant_id = ? AND resource_id = ? AND state = ?",
        (
            to,
            current.ref if ref is None else ref,
            *released,
            tenant_id,
            resource_id,
            current.state,
        ),
    )
    return _select(conn, tenant_id, resource_id)


def _select(conn: sqlite3.Connection, tenant_id: str, resource_id: str) -> Resource | None:
    found: tuple[object, ...] | None = conn.execute(
        f"SELECT {_COLUMNS} FROM resources WHERE tenant_id = ? AND resource_id = ?",  # noqa: S608 - fixed columns
        (tenant_id, resource_id),
    ).fetchone()
    return None if found is None else _row(found)


def _result(value: Resource | ParseError) -> Ok[Resource] | Err[ParseError]:
    return Err(value) if isinstance(value, ParseError) else Ok(value)


_KINDS: Final[Mapping[str, Kind]] = {"sandbox": "sandbox", "snapshot": "snapshot"}
_STATES: Final[Mapping[str, State]] = {
    s: s for s in ("pending", "live", "releasing", "released", "release_failed", "unknown")
}


def _row(values: tuple[object, ...]) -> Resource:
    rid, owner, provider, kind, ref, state, key, acquired, expires, released, outcome, claim = (
        values
    )
    return Resource(
        text_of(rid),
        BranchId(text_of(owner)),
        text_of(provider),
        _KINDS[text_of(kind)],
        None if ref is None else text_of(ref),
        _STATES[text_of(state)],
        text_of(key),
        int_of(acquired),
        None if expires is None else int_of(expires),
        None if released is None else int_of(released),
        None if outcome is None else text_of(outcome),
        None if claim is None else text_of(claim),
    )
