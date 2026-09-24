"""Hook points around a turn: before_input, before_model, after_model,
on_stop and stop_failure. Each gate's decisions are recorded, in the same batch as what they
lead to, before the work they gate. Whether a gate already ran is read from the log, so a
resumed run never asks a hook twice (replay reads recorded decisions)."""

from collections.abc import Mapping, Sequence
from typing import Final, Literal

from pydantic import JsonValue, TypeAdapter, ValidationError

from threads.hooks.runner import (
    INPUT,
    MODEL,
    NOTHING,
    RESPONSE,
    STOP,
    TEXTS,
    Ran,
    decision_draft,
    injected,
)
from threads.hooks.types import HookName, Source, wire_name
from threads.log import (
    Event,
    HookDecisionEvent,
    InjectedEvent,
    ModelRequestEvent,
    UserInputEvent,
)
from threads.loop.drafts import call_draft, draft
from threads.loop.history import last_response, response_calls, turn_events
from threads.loop.results import As, result_draft
from threads.loop.runtime import FAILED_CODES, Failed, Halt, Runtime, lost
from threads.result import Err
from threads.store import Draft

AGAIN: Final = "again"
type Gated = Halt | Literal["again"] | None
"""None: the gate passed; AGAIN: it appended, so the loop decides again; a Halt stops."""

MAX_RETRIES: Final = 2
"""after_model retry rounds per turn before a retry is a deny."""
MAX_STOP_CONTINUES: Final = 3
"""on_stop continuations per turn before stop_hook_limit."""
_STOPPED: Final = "on_stop"
_FIELDS: Final = TypeAdapter[Mapping[str, object]](Mapping[str, object])
_TEXTS: Final = TypeAdapter[list[str]](list[str])


def decided(events: Sequence[Event], hook: HookName, key: str, value: str) -> bool:
    """A decision of `hook` names this call, request or input."""
    return any(
        isinstance(e, HookDecisionEvent)
        and e.data.hook == wire_name(hook)
        and getattr(e.data, key) == value
        for e in events
    )


def _fields(ran: Ran[object]) -> Mapping[str, object]:
    try:
        return _FIELDS.validate_python(ran.value)
    except ValidationError:
        return {}


def verdict(ran: Ran[object], fail: str = "deny") -> str:
    """The decision a hook's answer stands for; a failure stands for `fail`."""
    decision = _fields(ran).get("decision")
    return decision if ran.failure is None and isinstance(decision, str) else fail


def said(ran: Ran[object], field: str) -> str | None:
    value = _fields(ran).get(field)
    return value if isinstance(value, str) else None


def texts(ran: Ran[object], field: str = "injections") -> list[str]:
    """Injections on an allow or proceed, or a hook's plain list of texts."""
    fields = _fields(ran)
    raw = fields.get(field, []) if fields else ran.value
    try:
        return _TEXTS.validate_python(raw)
    except ValidationError:
        return []


async def append(rt: Runtime, drafts: Sequence[Draft]) -> Gated:
    done = await rt.append(*drafts)
    return lost(done.error) if isinstance(done, Err) else AGAIN


async def before_request(rt: Runtime) -> Gated:
    """before_input for every input of the turn not yet decided, then before_model once per
    attempt."""
    turn = turn_events(rt.events)
    if rt.hooks.has("before_input"):
        for event in turn:
            if isinstance(event, UserInputEvent) and not decided(
                turn, "before_input", "input_event_id", event.event_id
            ):
                return await _before_input(rt, event)
    if rt.hooks.has("before_model") and not _model_gated(turn):
        return await _before_model(rt)
    return None


async def _before_input(rt: Runtime, event: UserInputEvent) -> Gated:
    """A deny keeps the input for audit, renders it in no request, and ends the turn
    input_denied."""
    ran = await rt.hooks.run("before_input", INPUT, event.data)
    denied = any(verdict(r) != "allow" for r in ran)
    ids = {"input_event_id": event.event_id}
    drafts = [decision_draft("before_input", r, verdict(r), said(r, "reason"), **ids) for r in ran]
    if denied:
        drafts.append(draft("turn_completed", {"reason": "input_denied"}))
    else:
        drafts += [d for r in ran for d in injected(r.extension, texts(r))]
    return await append(rt, drafts)


def _model_gated(turn: Sequence[Event]) -> bool:
    """before_model already decided this attempt: after the turn's latest turn request."""
    since = 0
    for index, event in enumerate(turn):
        if isinstance(event, ModelRequestEvent) and event.data.purpose != "compaction":
            since = index + 1
    return any(
        isinstance(e, HookDecisionEvent) and e.data.hook == "before_model" for e in turn[since:]
    )


async def _before_model(rt: Runtime) -> Gated:
    """A deny means the attempt is never sent; the turn ends error."""
    ran = await rt.hooks.run("before_model", MODEL, rt.writer.state())
    drafts = [decision_draft("before_model", r, verdict(r), said(r, "reason")) for r in ran]
    if any(verdict(r) != "proceed" for r in ran):
        drafts.append(draft("turn_completed", {"reason": "error"}))
    else:
        drafts += [d for r in ran for d in injected(r.extension, texts(r))]
    return await append(rt, drafts)


async def after_model(rt: Runtime) -> Gated:
    """Gates the response's undispatched calls and the release of its output.
    deny closes them denied and ends the turn with the output withheld; guide closes them and
    re-asks with a trusted instruction. ponytail: retry is treated as guide(reason): a fresh
    attempt that hides the recorded response needs a render rule the spec doesn't have yet."""
    turn = turn_events(rt.events)
    response = last_response(turn)
    if not rt.hooks.has("after_model") or response is None:
        return None
    request = response.data.request_event_id
    if decided(turn, "after_model", "request_event_id", request):
        return None
    ran = await rt.hooks.run("after_model", RESPONSE, rt.writer.state(), response.data)
    ids = {"request_event_id": request}
    drafts = [
        decision_draft("after_model", r, verdict(r), said(r, "reason") or said(r, "text"), **ids)
        for r in ran
    ]
    verdicts = {verdict(r) for r in ran}
    if verdicts == {"proceed"}:
        return await append(rt, drafts)
    # Retries are capped per turn, counted from the log so recovery can't reset them.
    denied = "deny" in verdicts or ("retry" in verdicts and _retries(turn) >= MAX_RETRIES)
    why = "denied by an output guardrail" if denied else "withheld: the response was guided"
    how = As("denied", True, "host")
    # An older writer recorded the calls with the response: they are pending already.
    for call_id in rt.fold.pending:
        drafts.append(await result_draft(rt, call_id, why, how))
    for use, call in response_calls(rt.events):
        if call is None:
            drafts += [call_draft(request, use), await result_draft(rt, use.call_id, why, how)]
    if denied:
        drafts.append(draft("turn_completed", {"reason": "error"}))
    else:
        guide = "\n".join(said(r, "text") or said(r, "reason") or "" for r in ran)
        drafts.append(_instruction("after_model", guide))
    return await append(rt, drafts)


def _retries(turn: Sequence[Event]) -> int:
    """after_model rounds that asked for a retry: one per gated response."""
    return len(
        {
            e.data.request_event_id
            for e in turn
            if isinstance(e, HookDecisionEvent)
            and e.data.hook == "after_model"
            and e.data.decision == "retry"
        }
    )


def _instruction(origin: str, text: str) -> Draft:
    data: dict[str, JsonValue] = {
        "source": "hook",
        "trust": "trusted_instruction",
        "origin": {"id": origin},
        "text": text,
    }
    return draft("injected", data)


async def end_turn(rt: Runtime) -> Halt | None:
    """The turn would end: on_stop may continue it, at most MAX_STOP_CONTINUES times; a failed
    on_stop stops."""
    if not rt.hooks.has("on_stop"):
        return await _done(rt, "end_turn")
    ran = await rt.hooks.run("on_stop", STOP, rt.writer.state())
    drafts = [decision_draft("on_stop", r, verdict(r, "stop"), said(r, "reason")) for r in ran]
    reasons = [said(r, "reason") or "" for r in ran if verdict(r, "stop") == "continue"]
    if not reasons:
        drafts.append(draft("turn_completed", {"reason": "end_turn"}))
    elif _continues(rt.events) >= MAX_STOP_CONTINUES:
        drafts.append(draft("turn_completed", {"reason": "stop_hook_limit"}))
    else:
        drafts.append(_instruction(_STOPPED, "\n".join(reasons)))
    done = await rt.append(*drafts)
    return lost(done.error) if isinstance(done, Err) else None


def _continues(events: Sequence[Event]) -> int:
    """Rounds of on_stop that continued the turn: one instruction each."""
    return sum(
        1
        for e in turn_events(events)
        if isinstance(e, InjectedEvent) and e.data.source == "hook" and e.data.origin.id == _STOPPED
    )


async def _done(rt: Runtime, reason: str) -> Halt | None:
    done = await rt.append(draft("turn_completed", {"reason": reason}))
    return lost(done.error) if isinstance(done, Err) else None


async def failed(rt: Runtime, reason: str) -> Halt | None:
    """stop_failure observers when a turn ends in a failure; on_stop doesn't run then."""
    code = FAILED_CODES.get(reason)
    return None if code is None else await observe(rt, "on_stop_failure", code)


async def observe(rt: Runtime, hook: HookName, *args: object, **ids: str) -> Halt | None:
    """An observe hook: it can't change execution, and only its failure is recorded."""
    if not rt.hooks.has(hook):
        return None
    ran = await rt.hooks.run(hook, NOTHING, *args)
    drafts = [decision_draft(hook, r, "failed", **ids) for r in ran if r.failure is not None]
    if not drafts:
        return None
    done = await rt.append(*drafts)
    return lost(done.error) if isinstance(done, Err) else None


async def session_start(rt: Runtime, source: Source) -> Halt | None:
    """Context hook: its injections go in before the input; a failure denies the session, so
    the run takes no input."""
    if not rt.hooks.has("session_start"):
        return None
    ran = await rt.hooks.run("session_start", TEXTS, source)
    drafts = [decision_draft("session_start", r, "proceed") for r in ran]
    failures = [f"{r.extension}: {r.failure}" for r in ran if r.failure is not None]
    if not failures:
        drafts += [d for r in ran for d in injected(r.extension, texts(r))]
    done = await rt.append(*drafts)
    if isinstance(done, Err):
        return lost(done.error)
    return (
        Failed("input_denied", f"session_start failed: {'; '.join(failures)}") if failures else None
    )
