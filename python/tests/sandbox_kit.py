"""Test-kit sandbox contexts for driving an adapter directly, outside any branch."""

from dataclasses import dataclass, field

from threads.log import BranchId, ParseError
from threads.result import Err, Ok
from threads.store.context import OwnerAuthority, SandboxAuthority

TEST_BRANCH = BranchId("0192b000-0000-7000-8000-0000000000ff")


@dataclass
class KitContext:
    """Passes its fence while `live`; counts how often the adapter checked it."""

    live: bool = True
    fences: int = 0
    authority: SandboxAuthority = field(default_factory=lambda: OwnerAuthority(TEST_BRANCH, 1))

    async def fence(self) -> Ok[None] | Err[ParseError]:
        self.fences += 1
        return Ok(None) if self.live else Err(ParseError("stale_epoch", "test: stale"))


OPEN = KitContext()
