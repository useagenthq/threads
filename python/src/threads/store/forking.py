"""The first lines of a child branch: its header and its `fork`."""

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import JsonValue

from threads.log import BranchId, Header, ParseError
from threads.log.digest import sha256_hex
from threads.reduce import Fold, apply, enter_segment
from threads.result import Err, Ok
from threads.store.lines import Draft, Position, event_line, header_line
from threads.store.sql import Branch
from threads.store.verify import StoredEvent, VerifiedLog


@dataclass(frozen=True, slots=True)
class ChildStart:
    row: Branch
    fork: tuple[StoredEvent, bytes]
    fold: Fold
    """The child's resolved chain, folded through its fork event."""
    epoch: int

    @property
    def runnable(self) -> bool:
        # A repair child has no sandbox matching its log.
        return self.row.state == "ready"


def start_child(
    prefix: VerifiedLog, child: BranchId, data: Mapping[str, JsonValue], now: int
) -> Ok[ChildStart] | Err[ParseError]:
    """Builds and validates a child of `prefix`'s last line. `data` is the fork payload without
    the parent link, which is derived here: parent_branch_id and at_hash."""
    fold, parent = prefix.fold, prefix.segments[-1]
    if fold.thread_id is None:
        raise ValueError("a verified prefix has a thread")
    header = header_line(fold.thread_id, child, now)
    link: dict[str, JsonValue] = {
        **data,
        "parent_branch_id": parent.header.branch_id,
        "at_hash": sha256_hex(parent.last_line),
    }
    # A new child continues its parent's epochs (wire rule 11).
    at = Position(fold.thread_id, child, fold.seq + 1, fold.epoch + 1, header, now)
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
        parent.header.branch_id,
        at.seq - 1,
        header,
        "inspection_only" if fold.repair else "ready",
        at.seq,
        sha256_hex(built.value[1]),
    )
    return Ok(ChildStart(row, built.value, fold, at.epoch))
