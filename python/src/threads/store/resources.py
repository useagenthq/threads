"""The resource ledger: provider resources a branch created, and their cleanup.

A `pending` row with a fresh `operation_key` is durable before the provider call that creates
the resource, so a lost answer or a crash resolves by that key and nothing is created twice.
Creating a row and starting a release are fenced by the owner's lease: a stale owner can do
neither. Every other move is a compare-and-set on the row's state along `MOVES`.
"""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from threads.log import BranchId, ParseError
from threads.result import Err, Ok
from threads.store import lease
from threads.store.lines import uuid7
from threads.store.sql import int_of, text_of, transaction
from threads.store.worker import Worker

type Kind = Literal["sandbox", "snapshot"]
type State = Literal["pending", "live", "releasing", "released", "release_failed", "unknown"]
type Move = Literal[
    "found", "not_found", "unresolved", "release", "released", "release_error", "retry"
]
"""What was learned about a row: found (it exists; `ref` is known), not_found (final: it never
existed or is already gone), unresolved (no final answer: an operator decides), release (start
releasing), released (the provider released it), release_error (it failed to), retry (gc tries
again). A row that ends released keeps the move that released it as `release_outcome`."""

MOVES: Final[Mapping[tuple[State, Move], State]] = {
    ("pending", "found"): "live",
    ("pending", "not_found"): "released",
    ("pending", "unresolved"): "unknown",
    # An operator records the ref, or confirms nothing exists.
    ("unknown", "found"): "live",
    ("unknown", "not_found"): "released",
    ("live", "release"): "releasing",
    # Reattaching after a restart (Sandbox.attach): gone for good, or no answer.
    ("live", "not_found"): "released",
    ("live", "unresolved"): "unknown",
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


_COLUMNS = (
    "resource_id, owner_branch_id, provider, kind, ref, state, operation_key, acquired_at,"
    " expires_at, released_at, release_outcome"
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

        def insert(conn: sqlite3.Connection) -> ParseError | None:
            with transaction(conn):
                stale = lease.check(conn, owner.branch_id, owner.lease, now)
                if stale is None:
                    _insert(conn, tenant, row)
                return stale

        error = await self._worker.call(insert)
        return Err(error) if error is not None else Ok(row)

    async def release(
        self, owner: lease.Owner, row: Resource, now: int
    ) -> Ok[Resource] | Err[ParseError]:
        """Moves the owner's live row to `releasing` before the provider is asked to release
        it. A stale owner is refused (stale_epoch)."""

        def start(conn: sqlite3.Connection) -> Resource | ParseError:
            with transaction(conn):
                stale = lease.check(conn, owner.branch_id, owner.lease, now)
                if stale is not None:
                    return stale
                if row.owner_branch_id != owner.branch_id:
                    return ParseError("stale_epoch", f"{row.resource_id} has another owner")
                moved = _move(conn, row.resource_id, "release", now, None)
            if moved is None:
                return ParseError("invalid_transition", f"{row.resource_id} is not live")
            return moved

        moved = await self._worker.call(start)
        return Err(moved) if isinstance(moved, ParseError) else Ok(moved)

    async def move(
        self, row: Resource, move: Move, now: int, ref: str | None = None
    ) -> Resource | None:
        """Applies `move` if it is legal from the row's current state; None when it is not
        (another process moved the row first). `found` records `ref`."""

        def apply(conn: sqlite3.Connection) -> Resource | None:
            with transaction(conn):
                return _move(conn, row.resource_id, move, now, ref)

        return await self._worker.call(apply)

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

    async def orphaned(self, now: int) -> tuple[Resource, ...]:
        """Pending rows whose owner holds no live lease: its creator crashed mid-call, so the
        row is resolved by its key. A live owner resolves its own rows."""

        def select(conn: sqlite3.Connection) -> tuple[Resource, ...]:
            found: list[tuple[object, ...]] = conn.execute(
                f"SELECT {_COLUMNS} FROM resources r WHERE tenant_id = ? AND state = 'pending'"  # noqa: S608 - fixed columns
                " AND NOT EXISTS (SELECT 1 FROM leases l WHERE l.branch_id = r.owner_branch_id"
                " AND l.expires_at > ?) ORDER BY rowid",
                (self._tenant, now),
            ).fetchall()
            return tuple(_row(values) for values in found)

        return await self._worker.call(select)


def _insert(conn: sqlite3.Connection, tenant_id: str, row: Resource) -> None:
    conn.execute(
        f"INSERT INTO resources (tenant_id, {_COLUMNS}) VALUES ({', '.join('?' * 12)})",  # noqa: S608 - fixed columns
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
        ),
    )


def _move(
    conn: sqlite3.Connection, resource_id: str, move: Move, now: int, ref: str | None
) -> Resource | None:
    row = _select(conn, resource_id)
    to = None if row is None else next_state(row.state, move)
    if row is None or to is None:
        return None
    released = (now, move) if to == "released" else (row.released_at, row.release_outcome)
    conn.execute(
        "UPDATE resources SET state = ?, ref = ?, released_at = ?, release_outcome = ?"
        " WHERE resource_id = ?",
        (to, row.ref if ref is None else ref, *released, resource_id),
    )
    return _select(conn, resource_id)


def _select(conn: sqlite3.Connection, resource_id: str) -> Resource | None:
    found: tuple[object, ...] | None = conn.execute(
        f"SELECT {_COLUMNS} FROM resources WHERE resource_id = ?",  # noqa: S608 - fixed columns
        (resource_id,),
    ).fetchone()
    return None if found is None else _row(found)


_KINDS: Final[Mapping[str, Kind]] = {"sandbox": "sandbox", "snapshot": "snapshot"}
_STATES: Final[Mapping[str, State]] = {
    s: s for s in ("pending", "live", "releasing", "released", "release_failed", "unknown")
}


def _row(values: tuple[object, ...]) -> Resource:
    rid, owner, provider, kind, ref, state, key, acquired, expires, released, outcome = values
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
    )
