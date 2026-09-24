"""A failed rebind when the worker resumes a member (spec/schema/README.md, "Teams", A failed
rebind): the definition a parked, stranded or woken member was started with is gone or changed in
this process. Its end is one append under its writer: each pending call whose effect never began
closes not_executed, an open turn closes {error, code}, and the member ends failed as any end does.
A call whose effect is in doubt keeps its record: it is never dispatched again."""

from pydantic import JsonValue

from threads.reduce import Fold
from threads.store.lines import Draft
from threads.team.materialize import RebindCode
from threads.team.provenance import turn_provenance
from threads.team.settle import AppendContext, SettleContext, settle


def rebind_failed(ctx: AppendContext, fold: Fold, code: RebindCode) -> None:
    """The member's end for a failed rebind, into the batch."""
    provenance = turn_provenance(ctx.conn, fold.events)
    if provenance is None:
        raise AssertionError("a member's log has a turn")
    in_doubt = {call for call, status in fold.effects.values() if status in ("begun", "unknown")}
    for call_id in fold.pending:
        if call_id in in_doubt:
            continue
        result: dict[str, JsonValue] = {
            "call_id": call_id,
            "is_error": True,
            "completeness": "complete",
            "origin": "not_executed",
            "preview": f"not executed: rebind failed: {code}",
        }
        ctx.batch.add(Draft("tool_result", result))
    if fold.in_turn:
        ctx.batch.add(Draft("turn_completed", {"reason": "error", "code": code}))
    failed: dict[str, JsonValue] = {
        "status": "failed",
        "error": {"code": code, "message": f"rebind failed: {code}"},
    }
    settle(
        SettleContext(ctx.conn, ctx.batch, ctx.thread_id, ctx.branch_id, provenance, _no_text),
        failed,
    )


def _no_text(_text: str) -> JsonValue:
    raise AssertionError("a failed rebind's result has no text")
