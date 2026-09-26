"""The model tools wait and monitor (spec/schema/README.md, "Teams", Waits and monitors; design
§4.12 and §4.13). Each reads its targets' rows in its own transaction: a settled target is recorded
as member_observed (its committed result, copied), any other gets a monitor row that its idle or
end append fires. Never both, never neither. Reference: spec/tools/fixtures/ops_observe.py."""

from collections.abc import Sequence
from typing import Literal

from pydantic import JsonValue

from threads.log import MemberRef, WaitStartedEvent
from threads.pos_int import is_pos_int
from threads.store.lines import Draft
from threads.team.call import (
    CallContext,
    Refusal,
    addressed,
    call_mail_id,
    call_request,
    caller_of,
    decide,
    decision,
    named,
    recorded,
)
from threads.team.close import CloseContext, finish_wait, public_result
from threads.team.constants import TEAM_CONSTANTS
from threads.team.park import park_call
from threads.team.request import Request, Target
from threads.team.rows import MemberRow, ref_of, settled_of

_SETTLED = frozenset({"idle", "ended"})


def _observe(ctx: CallContext | CloseContext, monitor_id: str, row: MemberRow) -> JsonValue:
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


type WaitMode = Literal["all", "any"] | int
"""How many listed members must settle: all, any (one), or a count."""


def wait(
    ctx: CallContext,
    members: Sequence[str],
    timeout_ms: int = TEAM_CONSTANTS.ask_wait_default_ms,
) -> JsonValue:
    """The model's wait (mode all, the default deadline): the listed members, a repeat dropped,
    then the wait. A re-dispatched wait only parks, and so does one left waiting."""
    caller = caller_of(ctx)
    wait_id = call_mail_id(ctx)
    again = any(
        isinstance(e, WaitStartedEvent) and e.data.wait_id == wait_id for e in ctx.fold.events
    )
    if not again:
        targets = [named(ctx, m) for m in dict.fromkeys(members)]
        got = open_wait(call_request(ctx), _closing(ctx), targets, "all", timeout_ms)
        if not isinstance(got, dict) or got.get("status") != "waiting":
            return got
    park_call(ctx, caller.provenance)
    return {"status": "waiting", "wait_id": wait_id}


def wait_members(
    members: Sequence[MemberRef], mode: WaitMode | None
) -> tuple[MemberRef, ...] | Literal["invalid_request"]:
    """An operator wait's members, a repeat dropped (they are frozen at the call);
    invalid_request for an empty list, or a mode that is neither "all", "any", nor a positive
    integer no greater than their count, refused before any writer is taken."""
    distinct = tuple(dict.fromkeys(members))
    if not distinct:
        return "invalid_request"
    if mode is None:
        return distinct
    if isinstance(mode, str):
        return distinct if mode in ("all", "any") else "invalid_request"
    bad = not is_pos_int(mode) or mode > len(distinct)
    return "invalid_request" if bad else distinct


def open_wait(
    req: Request,
    close: CloseContext,
    targets: Sequence[Target],
    mode: WaitMode,
    timeout_ms: int,
) -> JsonValue:
    """A wait, for a model call or an operator request: each target known at its generation,
    then one monitor decision each; wait_started, an observation per settled member, and the
    finish when that meets the mode. Returns what it recorded, or waiting."""
    rows: list[MemberRow] = []
    for target in targets:
        row = target.row()
        if isinstance(row, Refusal):
            return req.refuse(row)
        rows.append(row)
    for row in rows:
        denied = req.decide("monitor", row.name)
        if denied is not None:
            return req.refuse(denied)
    cap = TEAM_CONSTANTS.ask_wait_default_ms
    started_data: dict[str, JsonValue] = {
        "wait_id": req.mail_id,
        "members": [ref_of(req.team, r) for r in rows],
        "mode": mode,
        "deadline": req.batch.now + min(timeout_ms, cap),
    }
    started = req.batch.add(Draft("wait_started", started_data))
    for row in rows:
        if row.state in _SETTLED:
            _observe(close, f"{close.branch_id}:{started}:{row.name}", row)
    return finish_wait(close, req.mail_id, cause=None, deadline=False)


def monitor(ctx: CallContext, member: str) -> JsonValue:
    """monitor: its decision, then the member; monitor_set, and for an ended target its
    observation and the ended result at once, else an end monitor row whose firing opens a turn
    when idle."""
    caller = caller_of(ctx)
    denied = decide(ctx, "monitor", member, decision(ctx, caller, "monitor", member))
    if denied is not None:
        return recorded(ctx, denied)
    row = addressed(ctx, caller, member)
    if isinstance(row, Refusal):
        return recorded(ctx, row)
    ref = ref_of(caller.team, row)
    set_ = ctx.batch.add(Draft("monitor_set", {"member": ref}))
    if row.state != "ended":
        return recorded(ctx, {"member": ref, "status": "monitoring"})
    result = _observe(ctx, f"{ctx.call.branch_id}:{set_}:{row.name}", row)
    return recorded(ctx, {"result": public_result(result, ctx.read), "status": "ended"})
