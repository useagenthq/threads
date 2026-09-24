"""The model tools wait and monitor (spec/schema/README.md, "Teams", Waits and monitors; design
§4.12 and §4.13). Each reads its targets' rows in its own transaction: a settled target is recorded
as member_observed (its committed result, copied), any other gets a monitor row that its idle or
end append fires. Never both, never neither. Reference: spec/tools/fixtures/ops_observe.py."""

from collections.abc import Sequence

from pydantic import JsonValue

from threads.log import WaitStartedEvent
from threads.store.lines import Draft
from threads.team.call import (
    CallContext,
    Refusal,
    addressed,
    call_mail_id,
    caller_of,
    decide,
    recorded,
)
from threads.team.close import CloseContext, finish_wait, public_result
from threads.team.constants import TEAM_CONSTANTS
from threads.team.park import park_call
from threads.team.rows import MemberRow, ref_of, settled_of

_SETTLED = frozenset({"idle", "ended"})


def _observe(ctx: CallContext, monitor_id: str, row: MemberRow) -> JsonValue:
    """member_observed: the target's committed result, and where it is."""
    if row.branch_id is None:
        raise AssertionError("a settled member has a branch")
    settled = settled_of(ctx.conn, row)
    source: dict[str, JsonValue] = {
        "thread_id": row.thread_id,
        "branch_id": row.branch_id,
        "seq": settled.seq,
    }
    data: dict[str, JsonValue] = {
        "monitor_id": monitor_id,
        "result": settled.result,
        "source": source,
    }
    ctx.batch.add(Draft("member_observed", data))
    return settled.result


def _closing(ctx: CallContext) -> CloseContext:
    """The call's append as a close: the caller's own thread and branch."""
    call = ctx.call
    return CloseContext(ctx.conn, ctx.batch, call.thread_id, call.branch_id, ctx.fold, ctx.read)


def wait(
    ctx: CallContext,
    members: Sequence[str],
    timeout_ms: int = TEAM_CONSTANTS.ask_wait_default_ms,
) -> JsonValue:
    """wait (mode all, the default deadline): the listed members, a repeat dropped, each known at
    its generation, then one monitor decision each; wait_started, an observation per settled
    member, and the finish when that meets the mode, else a park. A re-dispatched wait only
    parks."""
    caller = caller_of(ctx)
    wait_id = call_mail_id(ctx)
    again = any(
        isinstance(e, WaitStartedEvent) and e.data.wait_id == wait_id for e in ctx.fold.events
    )
    if again:
        park_call(ctx, caller.provenance)
        return {"status": "waiting", "wait_id": wait_id}
    rows: list[MemberRow] = []
    for name in dict.fromkeys(members):
        row = addressed(ctx, caller, name)
        if isinstance(row, Refusal):
            return recorded(ctx, row)
        rows.append(row)
    for row in rows:
        decide(ctx, "monitor", row.name, allow=True)
    cap = TEAM_CONSTANTS.ask_wait_default_ms
    started_data: dict[str, JsonValue] = {
        "wait_id": wait_id,
        "members": [ref_of(caller.team, r) for r in rows],
        "mode": "all",
        "deadline": ctx.batch.now + min(timeout_ms, cap),
    }
    started = ctx.batch.add(Draft("wait_started", started_data))
    settled = [r for r in rows if r.state in _SETTLED]
    for row in settled:
        _observe(ctx, f"{ctx.call.branch_id}:{started}:{row.name}", row)
    if len(settled) == len(rows):
        return finish_wait(_closing(ctx), wait_id, cause=None, deadline=False)
    park_call(ctx, caller.provenance)
    return {"status": "waiting", "wait_id": wait_id}


def monitor(ctx: CallContext, member: str) -> JsonValue:
    """monitor: its decision, then the member; monitor_set, and for an ended target its
    observation and the ended result at once, else an end monitor row whose firing opens a turn
    when idle."""
    caller = caller_of(ctx)
    decide(ctx, "monitor", member, allow=True)
    row = addressed(ctx, caller, member)
    if isinstance(row, Refusal):
        return recorded(ctx, row)
    ref = ref_of(caller.team, row)
    set_ = ctx.batch.add(Draft("monitor_set", {"member": ref}))
    if row.state != "ended":
        return recorded(ctx, {"member": ref, "status": "monitoring"})
    result = _observe(ctx, f"{ctx.call.branch_id}:{set_}:{row.name}", row)
    return recorded(ctx, {"result": public_result(result, ctx.read), "status": "ended"})
