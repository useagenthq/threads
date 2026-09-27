"""What materialize takes and returns (spec/schema/README.md, "Teams"): shared by a started
member's open (materialize.py) and a host member's (materialize_host.py)."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import JsonValue

from threads.log import BranchId, MailEnvelope, MemberStartedEvent
from threads.store import Writer
from threads.store.worker import Clock
from threads.team.batch import Mint

type RebindCode = Literal["pin_unavailable", "pin_mismatch", "setup_failed"]

LINE_ZERO: Final = (
    "agent_name",
    "instructions",
    "model",
    "model_params",
    "adapter",
    "tools",
    "policy",
    "sandbox_provider",
)
"""thread_started takes the pinned config's line-0 fields; the rest of the config is hashed only."""


@dataclass(frozen=True, slots=True)
class Rebind:
    """What rebinding the member's definition by name found: ok (with a nested lead's own team,
    which its first append opens), or why not."""

    status: Literal["ok", "pin_unavailable", "pin_mismatch", "setup_failed"]
    team: Mapping[str, JsonValue] | None = None


@dataclass(frozen=True, slots=True)
class Materialized:
    status: Literal["materialized", "not_starting", "cancelled", "rebind_failed"]
    writer: Writer | None = None
    code: RebindCode | None = None


@dataclass(frozen=True, slots=True)
class MaterializeOptions:
    rebind: Callable[[MemberStartedEvent, MailEnvelope | None], Awaitable[Rebind]]
    """Rebinds the member's definition from its member_started and task (whose sender is the
    starter a dynamic member's block names). A host member has no task: None."""
    holder: str
    """The lease holder the member's first writer runs under."""
    ttl_ms: int
    clock: Clock
    mint: Mint | None = None
    branch_id: BranchId | None = None
    """The member's branch id; a new one by default."""
