"""Hook points around tool calls: before_tool and
permission_request fold into the call's one `permission_decision`; before_tool_result gates what
later requests render; after_tool_batch injects before the next request; after_tool and
permission_denied only observe."""

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Final

from threads.hooks.runner import RESULT, TEXTS, TOOL, Ran, decision_draft, injected
from threads.log import CallId, ToolCallEvent, ToolResultEvent
from threads.loop.drafts import draft
from threads.loop.gates import Gated, append, decided, last_response, said, texts, verdict
from threads.loop.history import turn_events
from threads.loop.runtime import Halt, Runtime, lost
from threads.permissions import Decision
from threads.permissions.engine import Verdict
from threads.reduce.handlers import to_json
from threads.reduce.redaction import first_text_part, span_error, text_part
from threads.result import Err
from threads.store import Draft

if TYPE_CHECKING:
    from pydantic import JsonValue

_RANK: Final[Mapping[Verdict, int]] = {"allow": 0, "ask": 1, "deny": 2}
_VERDICTS: Final[Mapping[str, Verdict]] = {"allow": "allow", "ask": "ask", "deny": "deny"}


def _hooked(ran: Sequence[Ran[object]]) -> Decision:
    """The strictest answer (deny absorbs ask absorbs allow; a failure denies) with the why of
    the first hook that gave it: a deny's reason, an ask's rule."""
    ranked: list[tuple[Verdict, Ran[object]]] = [
        (_VERDICTS.get(verdict(r), "deny"), r) for r in ran
    ]
    if not ranked:
        return Decision("allow", "hook")
    strictest, first = max(ranked, key=lambda pair: _RANK[pair[0]])
    if strictest == "allow":
        return Decision("allow", "hook")
    why = first.failure or said(first, "reason" if strictest == "deny" else "rule")
    return Decision(strictest, "hook", reason=why)


async def authorize(
    rt: Runtime, call: ToolCallEvent, policy: Decision
) -> tuple[Decision, list[Draft]]:
    """The call's decision with its hooks folded in. before_tool runs on every call; a policy
    deny stands whatever it says, and no hook can turn a deny or an ask into an allow except
    the permission_request approver, which only answers an ask."""
    ids = {"call_id": call.data.call_id}
    drafts: list[Draft] = []
    decision = policy
    if rt.hooks.has("before_tool"):
        ran = await rt.hooks.run("before_tool", TOOL, call.data)
        drafts += [
            decision_draft("before_tool", r, verdict(r), said(r, "reason"), **ids) for r in ran
        ]
        hooked = _hooked(ran)
        if policy.decision != "deny" and _RANK[hooked.decision] >= _RANK[policy.decision]:
            decision = hooked
    if decision.decision == "ask" and rt.hooks.has("permission_request"):
        ran = await rt.hooks.run("permission_request", TOOL, call.data)
        drafts += [
            decision_draft("permission_request", r, verdict(r), said(r, "reason"), **ids)
            for r in ran
        ]
        answered = _hooked(ran)
        # An ask answers nothing: the earlier decision and its reason stand.
        if answered.decision != "ask":
            decision = answered
    if decision.decision == "ask" and rt.fold.mode == "dont_ask":
        decision = Decision("deny", decision.source, decision.rule, decision.reason)
    return decision, drafts


async def after_tool(rt: Runtime, call_id: CallId) -> Halt | None:
    """Observation only: annotations and failures are recorded; the effect already happened
    and is never re-run (F6.4)."""
    result = _result(rt, call_id)
    if not rt.hooks.has("after_tool") or result is None:
        return None
    call = rt.fold.calls[call_id]
    ran = await rt.hooks.run("after_tool", TEXTS, call.data, result.data)
    ids = {"call_id": call_id}
    drafts = [
        decision_draft("after_tool", r, "annotate", "\n".join(texts(r)), **ids)
        for r in ran
        if r.failure is not None or texts(r)
    ]
    if not drafts:
        return None
    done = await rt.append(*drafts)
    return lost(done.error) if isinstance(done, Err) else None


def _result(rt: Runtime, call_id: CallId) -> ToolResultEvent | None:
    return next(
        (
            e
            for e in reversed(rt.events)
            if isinstance(e, ToolResultEvent) and e.data.call_id == call_id
        ),
        None,
    )


async def before_results(rt: Runtime) -> Gated:
    """Each result of the turn passes before_tool_result before any request renders it."""
    if not rt.hooks.has("before_tool_result"):
        return None
    turn = turn_events(rt.events)
    for event in turn:
        if isinstance(event, ToolResultEvent) and not decided(
            turn, "before_tool_result", "call_id", event.data.call_id
        ):
            return await _result_gate(rt, event)
    return None


async def _result_gate(rt: Runtime, result: ToolResultEvent) -> Gated:
    """redact appends context_edited{guardrail, redact} on the first text part; deny, a failure
    or spans that don't fit that text clear the result from every later request. The raw result
    stays in the log."""
    call_id = result.data.call_id
    call = rt.fold.calls[call_id]
    ran = await rt.hooks.run("before_tool_result", RESULT, call.data, result.data)
    ids = {"call_id": call_id}
    part = first_text_part(result.data)
    text = None if part is None else text_part(result.data, part)
    drafts: list[Draft] = []
    spans: list[JsonValue] = []
    clear = False
    for r in ran:
        decided, why = verdict(r), said(r, "reason")
        if r.value is not None and r.value["decision"] == "redact":
            asked = r.value["spans"]
            if not asked or text is None or span_error(text, asked) is not None:
                decided, why = "failed", "redaction spans outside the result's text"
            else:
                spans += [to_json(span) for span in asked]
        clear = clear or decided in ("deny", "failed")
        drafts.append(decision_draft("before_tool_result", r, decided, why, **ids))
    edit: dict[str, JsonValue] | None = None
    if clear:
        edit = {"call_id": call_id, "action": "clear"}
    elif spans:
        edit = {"call_id": call_id, "action": "redact", "part": part, "spans": spans}
    if edit is not None:
        drafts.append(draft("context_edited", {"reason": "guardrail", "edits": [edit]}))
    return await append(rt, drafts)


async def after_batch(rt: Runtime) -> Gated:
    """Every result of one response is in: after_tool_batch injections go in before the next
    request, which waits for them; a failure denies that request (the turn ends error)."""
    turn = turn_events(rt.events)
    response = last_response(turn)
    if not rt.hooks.has("after_tool_batch") or response is None or rt.fold.pending:
        return None
    request = response.data.request_event_id
    if decided(turn, "after_tool_batch", "request_event_id", request):
        return None
    ran = await rt.hooks.run("after_tool_batch", TEXTS, rt.writer.state())
    ids = {"request_event_id": request}
    drafts = [decision_draft("after_tool_batch", r, "proceed", **ids) for r in ran]
    if any(r.failure is not None for r in ran):
        drafts.append(draft("turn_completed", {"reason": "error"}))
    else:
        drafts += [d for r in ran for d in injected(r.extension, texts(r))]
    return await append(rt, drafts)
