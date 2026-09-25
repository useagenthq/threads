# pyright: strict
"""A tenant's host team ids (spec/schema/README.md, "Teams Phase 2"), and the tenant checks the
reference validator makes with them (rules 50 and 52)."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from .common import obj, text

if TYPE_CHECKING:
    from .jcs import Obj

# What the host member's failed turn records, by its turn_completed (rule 53): the reason, or for
# error its code (else model_error). end_turn and cancelled are not failures; a handoff can't
# happen (setup refuses handoffs on a host member).
PASSED_THROUGH = frozenset(
    {
        "max_turns", "max_output", "stop_hook_limit", "context_exhausted", "output_invalid",
        "input_denied", "model_unavailable", "budget_exhausted", "interrupted",
    }
)  # fmt: skip


def turn_failure_code(turn_end: Obj) -> str:
    reason = text(turn_end["reason"])
    if reason == "error":
        return text(turn_end.get("code", "model_error"))
    if reason not in PASSED_THROUGH:
        raise ValueError(f"turn_completed{{{reason}}} is no host member turn failure")
    return reason


def _lp(s: str) -> bytes:
    b = s.encode()
    return len(b).to_bytes(4, "big") + b


def derived(part: str, tenant: str) -> str:
    """UUIDv8 of sha256(lp("threads/host-team") ‖ lp(part) ‖ lp(tenant)), for part team,
    log_thread or log_branch: every process derives the same ids, so a concurrent lazy open is
    already_open."""
    raw = b"".join(map(_lp, ("threads/host-team", part, tenant)))
    digest = bytearray(hashlib.sha256(raw).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x80
    digest[8] = (digest[8] & 0x3F) | 0x80
    h = digest.hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def opened_at_derived_ids(e: Obj) -> bool:
    """Rule 50: a host team's log is at the ids its tenant derives."""
    tenant = text(obj(e["data"])["tenant"])
    here = (obj(e["data"])["team"], e["thread_id"], e["branch_id"])
    return here == tuple(derived(p, tenant) for p in ("team", "log_thread", "log_branch"))


def tenant_team(env: Obj) -> bool:
    """Rule 52: host team mail belongs to the team its provenance principal's tenant derives."""
    tenant = text(obj(obj(env["provenance"])["principal"])["tenant"])
    return env["team"] == derived("team", tenant)
