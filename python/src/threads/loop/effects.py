"""Effect execution and settlement: `effect_begin` is durable before dispatch, and a
dispatched attempt stays potentially sent until a proof settles it. Anything else parks.

Only these proofs settle an uncertain effect here: the sandbox confirms the process group is
gone (`sandbox_local`), the provider dedups a re-send inside its window (`idempotent`), or a
final lookup answers (`reconcilable`). Elapsed time and lease expiry prove nothing.
"""

import asyncio
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Final, Literal

from threads.log import ToolSpec
from threads.loop.drafts import ActorKind, draft
from threads.loop.history import CallState, call_state
from threads.loop.model import Found, NotFound, looked_up
from threads.loop.results import As, result_draft, text_ref
from threads.loop.runtime import Barred, Failed, Halt, Parked, Runtime, fence, lost
from threads.loop.tools import Invocation, NotSent, Output, Uncertain
from threads.result import Err

if TYPE_CHECKING:
    from pydantic import JsonValue

DAY_MS: Final = 86_400_000
"""Default TTL of a parked effect."""
SKEW_MS: Final = 1000
"""The adapter's declared clock skew margin for dedup windows; doubled on the host clock."""


def invocation(state: CallState, spec: ToolSpec) -> Invocation:
    call = state.call
    # The derived effect key (wire rule 13): the tool_call's branch and its call_id.
    key = f"{call.branch_id}:{call.data.call_id}"
    return Invocation(spec, call.data.call_id, call.data.input, key)


async def dispatch(rt: Runtime, state: CallState, spec: ToolSpec) -> Halt | None:
    """Begins attempt n + 1 durably, then dispatches it and records what is known."""
    inv = invocation(state, spec)
    begin = {"call_id": inv.call_id, "attempt": len(state.begins) + 1}
    begun = await rt.append(draft("effect_begin", begin))
    if isinstance(begun, Err):
        return lost(begun.error)
    if isinstance(begun, Barred):
        # A cancel landed first: nothing is dispatched, and the cancellation step closes the call.
        return None
    stale = await fence(rt)
    return stale if stale is not None else await _dispatched(rt, inv, spec)


async def _dispatched(rt: Runtime, inv: Invocation, spec: ToolSpec) -> Halt | None:
    """Dispatches the begun attempt and records what is known of it."""
    match await rt.tools.dispatch(inv):
        case Output() as output:
            return await _settled(_commit(rt, inv, output))
        case Uncertain(reason=reason):
            unknown = await rt.append(
                draft("effect_unknown", {"call_id": inv.call_id, "reason": reason})
            )
            if isinstance(unknown, Err):
                return lost(unknown.error)
            # Settle on the state as recorded now, with this attempt's effect_begin in it.
            fresh = call_state(rt.events, inv.call_id)
            return await settle(rt, fresh, spec, reason, "host")
        case NotSent(unmatched=unmatched):
            return await _not_sent(rt, inv, unmatched=unmatched)


async def _commit(rt: Runtime, inv: Invocation, output: Output) -> Halt | None:
    ref = await text_ref(rt, output.text)
    commit = draft("effect_commit", {"call_id": inv.call_id, "result_ref": ref})
    how = As("executed", output.is_error)
    result = await result_draft(
        rt, inv.call_id, output.text, how, output.full_output, content=output.content
    )
    done = await rt.append(commit, result)
    return lost(done.error) if isinstance(done, Err) else None


async def _settled(record: Coroutine[object, object, Halt | None]) -> Halt | None:
    """Records a known outcome even if the run is cancelled meanwhile (a stopping host): the
    effect happened, so it is settled under this lease before the run unwinds, never left in
    doubt for a lookup or a human."""
    recording = asyncio.ensure_future(record)
    try:
        return await asyncio.shield(recording)
    except asyncio.CancelledError:
        await recording
        raise


async def _not_sent(rt: Runtime, inv: Invocation, *, unmatched: bool) -> Halt | None:
    resolved = draft(
        "effect_resolved", {"call_id": inv.call_id, "outcome": "not_sent", "by": "adapter"}
    )
    if unmatched:
        # Stub mode fails closed: settled, never live, and the run fails.
        done = await rt.append(resolved)
        if isinstance(done, Err):
            return lost(done.error)
        return Failed("unmatched_external_op", f"no recorded stub for {inv.spec.name}")
    text = "not sent: the provider never received the request"
    result = await result_draft(rt, inv.call_id, text, As("not_executed", True))
    done = await rt.append(resolved, result)
    return lost(done.error) if isinstance(done, Err) else None


async def settle(
    rt: Runtime, state: CallState, spec: ToolSpec, reason: str, actor: ActorKind
) -> Halt | None:
    """Applies the class rule to an effect in doubt: settle it with a proof, or park it."""
    inv = invocation(state, spec)
    match spec.effect_class:
        case "sandbox_local":
            settled = await _terminate(rt, inv, reason, actor)
        case "idempotent":
            settled = await _dedup(rt, state, spec, actor)
        case "reconcilable":
            settled = await _reconcile(rt, inv, actor)
        case _:
            settled = False
    if settled is not False:
        return settled
    return await park_effect(rt, inv, actor)


async def _terminate(
    rt: Runtime, inv: Invocation, reason: str, actor: ActorKind
) -> Halt | Literal[False] | None:
    stale = await fence(rt)
    if stale is not None:
        return stale
    if await rt.tools.terminate(inv) not in ("terminated", "already_exited"):
        return False
    why = "timed out" if reason == "timeout" else "the run stopped"
    text = f"interrupted: {why}; the command may have partly run"
    resolved = {"call_id": inv.call_id, "outcome": "interrupted", "by": "sandbox_terminated"}
    result = await result_draft(rt, inv.call_id, text, As("interrupted", True, actor))
    done = await rt.append(draft("effect_resolved", resolved, actor), result)
    return lost(done.error) if isinstance(done, Err) else None


async def _dedup(
    rt: Runtime, state: CallState, spec: ToolSpec, actor: ActorKind
) -> Halt | Literal[False] | None:
    window = spec.dedup_window_ms
    provider_now = rt.tools.provider_now()
    now, skew = (rt.clock(), 2 * SKEW_MS) if provider_now is None else (provider_now, SKEW_MS)
    if not isinstance(window, int) or not state.begins:
        return False
    # The window runs from the FIRST attempt under this key: a deduped re-send doesn't renew
    # the provider's retention, so measuring from a later begin could outlive the key.
    if now - state.begins[0].time > window - skew:
        return False
    data = {"call_id": state.call.data.call_id, "outcome": "safe_to_retry", "by": "provider_dedup"}
    done = await rt.append(draft("effect_resolved", data, actor))
    return lost(done.error) if isinstance(done, Err) else None


async def _reconcile(
    rt: Runtime, inv: Invocation, actor: ActorKind
) -> Halt | Literal[False] | None:
    stale = await fence(rt)
    if stale is not None:
        return stale
    match await looked_up(rt.tools.lookup(inv)):
        case Found(value=text):
            ref = await text_ref(rt, text)
            data: dict[str, JsonValue] = {
                "call_id": inv.call_id,
                "outcome": "confirmed_success",
                "by": "reconcile",
                "result_ref": ref,
            }
            result = await result_draft(rt, inv.call_id, text, As("executed", actor=actor))
            done = await rt.append(draft("effect_resolved", data, actor), result)
        case NotFound():
            data = {"call_id": inv.call_id, "outcome": "safe_to_retry", "by": "reconcile"}
            done = await rt.append(draft("effect_resolved", data, actor))
        case _:
            return False
    return lost(done.error) if isinstance(done, Err) else None


async def park_effect(rt: Runtime, inv: Invocation, actor: ActorKind) -> Halt | None:
    address: JsonValue = {"kind": "effect", "id": inv.effect_key}
    data: dict[str, JsonValue] = {
        "address": address,
        "reason": "effect_unknown",
        "expires_at": rt.clock() + DAY_MS,
    }
    done = await rt.append(draft("parked", data, actor))
    if isinstance(done, Err):
        return lost(done.error)
    return Parked("effect_unknown", tuple(rt.fold.parked))


async def close_settled(rt: Runtime, state: CallState, actor: ActorKind) -> Halt | None:
    """A call whose effect is settled for good but has no result yet: the result comes from the
    settlement, and the tool never runs again. Only safe_to_retry, not_sent and assume_not_done
    may lead to another dispatch."""
    match state.effect:
        case "committed":
            return await _materialize(rt, state, As("materialized_from_commit", actor=actor))
        case "confirmed_success":
            # The effect was proven performed: its recorded result stands as executed.
            return await _materialize(rt, state, As("executed", actor=actor))
        case "assume_done":
            text = "done: an approver resolved the uncertain effect as performed"
            how = As("executed", actor=actor)
        case "interrupted":
            text = "interrupted: the command may have partly run"
            how = As("interrupted", True, actor)
        case status:
            raise AssertionError(f"effect status {status} has no settled result")
    result = await result_draft(rt, state.call.data.call_id, text, how)
    done = await rt.append(result)
    return lost(done.error) if isinstance(done, Err) else None


async def _materialize(rt: Runtime, state: CallState, how: As) -> Halt | None:
    """The result from the settlement's artifact; the tool never runs again."""
    ref = state.commit
    if ref is None:
        raise AssertionError("a committed or confirmed effect records its result")
    got = await rt.store.get_artifact(ref.sha256)
    if isinstance(got, Err):
        missing = got.error.code == "artifact_missing"
        return Failed("artifact_missing" if missing else "artifact_corrupt", got.error.message)
    result = await result_draft(
        rt, state.call.data.call_id, got.value.decode("utf-8", "replace"), how
    )
    done = await rt.append(result)
    return lost(done.error) if isinstance(done, Err) else None
