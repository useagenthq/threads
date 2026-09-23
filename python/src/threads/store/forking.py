"""A child branch in two steps: `forking` with its header and lease, then its
`fork` event and final state once whatever it restores is in hand."""

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import JsonValue

from threads.log import BranchId, Header, ParseError
from threads.log.digest import sha256_hex
from threads.reduce import Fold, apply, enter_segment
from threads.result import Err, Ok
from threads.store.lease import TTL_MS, Lease, Owner
from threads.store.lines import Draft, Position, event_line, header_line
from threads.store.sql import Branch
from threads.store.verify import StoredEvent, VerifiedLog


@dataclass(frozen=True, slots=True)
class Forking:
    """A child whose `forking` row and lease are stored and whose `fork` event is not."""

    prefix: VerifiedLog
    """The parent's resolved chain through the fork point. `start_child` folds the child's
    fork into it, once."""
    row: Branch
    owner: Owner


def forking(
    prefix: VerifiedLog, tenant_id: str, child: BranchId, holder_id: str, now: int
) -> Forking:
    """The child's `forking` row and first lease. A new child continues its parent's epochs
    (wire rule 11); nothing reads a forking row's head, so it names the header."""
    fold, parent = prefix.fold, prefix.segments[-1]
    if fold.thread_id is None:
        raise ValueError("a verified prefix has a thread")
    header = header_line(fold.thread_id, child, now)
    row = Branch(
        child,
        fold.thread_id,
        tenant_id,
        parent.header.branch_id,
        fold.seq,
        header,
        "forking",
        fold.seq,
        sha256_hex(header),
    )
    return Forking(prefix, row, Owner(child, Lease(holder_id, fold.epoch + 1, now + TTL_MS)))


@dataclass(frozen=True, slots=True)
class ChildStart:
    row: Branch
    fork: tuple[StoredEvent, bytes]
    fold: Fold
    """The child's resolved chain, folded through its fork event."""

    @property
    def runnable(self) -> bool:
        # A repair child has no sandbox matching its log.
        return self.row.state == "ready"


def start_child(
    started: Forking, data: Mapping[str, JsonValue], now: int
) -> Ok[ChildStart] | Err[ParseError]:
    """Builds and validates the child's `fork` event. `data` is the fork payload without the
    parent link, which is derived here: parent_branch_id and at_hash."""
    prefix, header = started.prefix, started.row.header_line
    fold, parent = prefix.fold, prefix.segments[-1]
    if fold.thread_id is None:
        raise ValueError("a verified prefix has a thread")
    link: dict[str, JsonValue] = {
        **data,
        "parent_branch_id": parent.header.branch_id,
        "at_hash": sha256_hex(parent.last_line),
    }
    child = started.row.branch_id
    at = Position(fold.thread_id, child, fold.seq + 1, started.owner.lease.epoch, header, now)
    built = event_line(Draft("fork", link), at)
    if isinstance(built, Err):
        return built
    enter_segment(fold, Header.model_validate_json(header))
    error = apply(fold, built.value[0])
    if error is not None:
        return Err(error)
    row = Branch(
        child,
        fold.thread_id,
        started.row.tenant_id,
        parent.header.branch_id,
        at.seq - 1,
        header,
        "inspection_only" if fold.repair else "ready",
        at.seq,
        sha256_hex(built.value[1]),
    )
    return Ok(ChildStart(row, built.value, fold))
