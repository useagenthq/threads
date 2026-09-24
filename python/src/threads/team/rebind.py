"""A failed rebind when the worker resumes a member (spec/schema/README.md, "Teams", A failed
rebind): the definition a parked, stranded or woken member was started with is gone or changed in
this process. One append under its writer. A call whose effect is begun or unknown is in doubt
(invariant 3): the member parks on that effect, as recovery does, and ends only once a human
settled it. Otherwise each pending call closes from its record (not_executed when its effect never
began or was settled not done), an open turn closes {error, code}, and the member ends failed as
any end does."""

from collections.abc import Sequence
from typing import Final, Literal, assert_never

from pydantic import JsonValue

from threads.log import ArtifactRef, EffectCommitEvent, EffectResolvedEvent, ParkedEvent
from threads.reduce import Fold
from threads.reduce.fold import EffectStatus, loop_pending
from threads.reduce.handlers import to_json
from threads.store.lines import Draft
from threads.team.materialize import RebindCode
from threads.team.park import park_notice
from threads.team.provenance import turn_provenance
from threads.team.settle import AppendContext, SettleContext, settle

DAY_MS: Final = 24 * 60 * 60 * 1000


def rebind_failed(ctx: AppendContext, fold: Fold, code: RebindCode, now: int) -> None:
    """The member's end for a failed rebind, or its park on an effect in doubt, into the batch."""
    provenance = turn_provenance(ctx.conn, fold.events)
    if provenance is None:
        raise AssertionError("a member's log has a turn")
    # The host's own sends are the host's to settle, never a member's rebind's.
    doubt = [c for c in loop_pending(fold) if _status(fold, ctx, c) in ("begun", "unknown")]
    if doubt:
        _park_on(ctx, fold, doubt, provenance, now)
        return
    for call_id in loop_pending(fold):
        ctx.batch.add(_closing(fold, call_id, _status(fold, ctx, call_id), code))
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


def _key(ctx: AppendContext, call_id: str) -> str:
    return f"{ctx.branch_id}:{call_id}"


def _status(fold: Fold, ctx: AppendContext, call_id: str) -> EffectStatus | None:
    found = fold.effects.get(_key(ctx, call_id))
    return None if found is None else found[1]


def _park_on(
    ctx: AppendContext, fold: Fold, calls: Sequence[str], provenance: JsonValue, now: int
) -> None:
    """Parks on each in-doubt effect not parked on yet; the first park of the log notifies."""
    first = not any(isinstance(e, ParkedEvent) for e in fold.events)
    for call_id in calls:
        if _status(fold, ctx, call_id) == "begun":
            unknown: dict[str, JsonValue] = {"call_id": call_id, "reason": "crash_after_begin"}
            ctx.batch.add(Draft("effect_unknown", unknown, {"kind": "recovery"}))
        key = _key(ctx, call_id)
        if any(a.kind == "effect" and a.id == key for a in fold.parked):
            continue
        parked: dict[str, JsonValue] = {
            "address": {"kind": "effect", "id": key},
            "reason": "effect_unknown",
            "expires_at": now + DAY_MS,
        }
        event_id = ctx.batch.add(Draft("parked", parked, {"kind": "recovery"}))
        if first:
            park_notice(ctx, provenance, event_id, "effect_unknown")
        first = False


def _closing(fold: Fold, call_id: str, status: EffectStatus | None, code: RebindCode) -> Draft:
    """A pending call's tool_result from its record alone: it is never dispatched again."""
    not_executed = _result(call_id, "not_executed", f"not executed: rebind failed: {code}", True)
    if status is None:
        return not_executed
    last = next(
        (
            e
            for e in reversed(fold.events)
            if isinstance(e, EffectResolvedEvent | EffectCommitEvent) and e.data.call_id == call_id
        ),
        None,
    )
    if isinstance(last, EffectCommitEvent):
        preview = "result recorded by its commit"
        return _result(call_id, "materialized_from_commit", preview, False, last.data.result_ref)
    return not_executed if last is None else _resolved(call_id, last, not_executed)


def _resolved(call_id: str, last: EffectResolvedEvent, not_executed: Draft) -> Draft:
    """The result a human's or a reconciliation's settlement records."""
    data = last.data
    match data.outcome:
        case "safe_to_retry" | "not_sent" | "assume_not_done":
            return not_executed
        case "interrupted":
            preview = "interrupted: the command may have partly run"
            return _result(call_id, "interrupted", preview, True)
        case "assume_done":
            return _result(call_id, "executed", "assumed done by an approver", False)
        case "confirmed_success":
            ref = data.result_ref if isinstance(data.result_ref, ArtifactRef) else None
            return _result(call_id, "executed", "confirmed by an approver", False, ref)
        case _:
            assert_never(data.outcome)


def _result(
    call_id: str,
    origin: Literal["executed", "interrupted", "materialized_from_commit", "not_executed"],
    preview: str,
    is_error: bool,
    ref: ArtifactRef | None = None,
) -> Draft:
    data: dict[str, JsonValue] = {
        "call_id": call_id,
        "is_error": is_error,
        "completeness": "complete",
        "origin": origin,
        "preview": preview,
    }
    if ref is not None:
        data["ref"] = to_json(ref)
    return Draft("tool_result", data)


def _no_text(_text: str) -> JsonValue:
    raise AssertionError("a failed rebind's result has no text")
