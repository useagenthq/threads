"""A tenant's host team (spec/schema/README.md, "Teams Phase 2"): its derived ids, and the code a
host member's failed turn records (rule 53). The reference is spec/tools/fixtures/host_ids.py.

The ids are derived, so two processes racing the lazy open collide on the primary key and the
loser gets `already_open`, and a rebuild finds the host team's logs without a lead to start from.
"""

from dataclasses import dataclass
from functools import lru_cache
from typing import Final

from pydantic.experimental.missing_sentinel import MISSING

from threadsai.log import BranchId, ThreadId, TurnCompletedData
from threadsai.log.derive import uuid_v8

_NAMESPACE: Final = "threads/host-team"

PASSED_THROUGH: Final = frozenset(
    {
        "max_turns",
        "max_output",
        "stop_hook_limit",
        "context_exhausted",
        "output_invalid",
        "input_denied",
        "model_unavailable",
        "budget_exhausted",
        "interrupted",
    }
)
"""Turn endings a host member's TurnFailure.code passes through unchanged (rule 53's table)."""

TURN_KEPT: Final = frozenset({"end_turn", "cancelled"})
"""A host member's turn that ends any other way failed: only that turn ends (rule 53)."""


@dataclass(frozen=True, slots=True)
class HostTeamIds:
    """The three ids a tenant derives: its host team, and that team's log thread and branch."""

    team: str
    log_thread_id: ThreadId
    log_branch_id: BranchId


@lru_cache(maxsize=64)
def host_team_ids(tenant: str) -> HostTeamIds:
    """UUIDv8(sha256(lp("threads/host-team") | lp(part) | lp(tenant))) for each part (rule 50)."""
    team, thread, branch = (uuid_v8(_NAMESPACE, p, tenant) for p in _PARTS)
    return HostTeamIds(team, ThreadId(thread), BranchId(branch))


_PARTS: Final = ("team", "log_thread", "log_branch")


def turn_failure_code(end: TurnCompletedData) -> str | None:
    """A failed turn's TurnFailure.code: the reason, or for `error` its code (else model_error).
    None when the turn did not fail (end_turn, cancelled) or cannot (handoff)."""
    if end.reason == "error":
        return "model_error" if end.code is MISSING else end.code
    return end.reason if end.reason in PASSED_THROUGH else None
