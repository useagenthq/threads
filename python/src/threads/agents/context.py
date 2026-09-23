"""`RunContext` (spec/api.json): what a tool body sees about its run. Deps are host-only and never
serialized."""

from dataclasses import dataclass

from threads.log import BranchId, CallId, Principal, ThreadId


@dataclass(frozen=True, slots=True)
class RunContext[D]:
    deps: D
    thread_id: ThreadId
    branch_id: BranchId
    principal: Principal
    call_id: CallId | None = None
    """Set inside a tool."""
    effect_key: str | None = None
    """`<branch_id>:<call_id>`: send it to providers that dedup."""
