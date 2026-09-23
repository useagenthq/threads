"""The fence a sandbox adapter re-checks at its provider dispatch point (spec/api.json
`SandboxContext`, `SandboxAuthority`). Bound to one authority; nothing on it
selects another."""

from dataclasses import dataclass
from typing import Literal

from threads.log import BranchId, ParseError
from threads.result import Err, Ok
from threads.store import lease
from threads.store.resources import Ledger, Resource
from threads.store.worker import Clock, Worker


@dataclass(frozen=True, slots=True)
class OwnerAuthority:
    branch_id: BranchId
    epoch: int
    kind: Literal["owner"] = "owner"


@dataclass(frozen=True, slots=True)
class CleanupAuthority:
    resource_id: str
    claim: str
    kind: Literal["cleanup"] = "cleanup"


type SandboxAuthority = OwnerAuthority | CleanupAuthority


class OwnerContext:
    """An owner's operations pass while its lease is still live at its epoch."""

    def __init__(self, worker: Worker, owner: lease.Owner, clock: Clock) -> None:
        self._worker = worker
        self._owner = owner
        self._clock = clock
        self.authority: SandboxAuthority = OwnerAuthority(owner.branch_id, owner.lease.epoch)

    async def fence(self) -> Ok[None] | Err[ParseError]:
        owner, now = self._owner, self._clock()
        error = await self._worker.call(lambda c: lease.check(c, owner.branch_id, owner.lease, now))
        return Ok(None) if error is None else Err(error)


class CleanupContext:
    """gc's operations on one claimed row pass while the row still carries this claim and is
    still collectable; they need no branch, so they work after the owner is deleted."""

    def __init__(self, ledger: Ledger, row: Resource, clock: Clock) -> None:
        if row.cleanup_claim is None:
            raise ValueError("a cleanup context needs a claimed row")
        self._ledger = ledger
        self._clock = clock
        self.authority: SandboxAuthority = CleanupAuthority(row.resource_id, row.cleanup_claim)

    async def fence(self) -> Ok[None] | Err[ParseError]:
        auth = self.authority
        if not isinstance(auth, CleanupAuthority):
            raise TypeError("a cleanup context holds a cleanup authority")
        if await self._ledger.holds(auth.resource_id, auth.claim, self._clock()):
            return Ok(None)
        return Err(ParseError("stale_epoch", f"claim {auth.claim} on {auth.resource_id} is gone"))
