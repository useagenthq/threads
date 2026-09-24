"""A response's tool calls, recorded in part order (spec/schema/README.md, "Recording a
response's calls"): each `tool_call` is followed by its error result or its authorization before
the next part is recorded, and no call runs until every part is. A run stopped part-way finishes
the recording first, so its log equals an uninterrupted run's."""

from typing import TYPE_CHECKING

from threads.log import CallId, EventId, ToolUsePart
from threads.loop import calls, output, search
from threads.loop.drafts import call_draft, draft
from threads.loop.history import (
    call_state,
    last_response,
    open_cancel,
    response_calls,
    turn_events,
)
from threads.loop.runtime import Halt, Runtime, lost
from threads.reduce.fold import loop_pending, still_deferred
from threads.result import Err
from threads.store import Draft

if TYPE_CHECKING:
    from pydantic import JsonValue


def owed(rt: Runtime) -> bool:
    """The latest response has a part not yet recorded, or a recorded call not yet authorized
    (a log written before calls were authorized as they were recorded included)."""
    return any(
        call is None or _undecided(rt, call.data.call_id) for _, call in response_calls(rt.events)
    )


def _undecided(rt: Runtime, call_id: CallId) -> bool:
    return call_id in loop_pending(rt.fold) and call_state(rt.events, call_id).decision is None


async def record_calls(rt: Runtime) -> Halt | None:
    """Records and authorizes every call of the latest response still owed it, in part order."""
    response = last_response(turn_events(rt.events))
    if response is None:
        return None
    for use, call in response_calls(rt.events):
        if open_cancel(rt.events) is not None:
            return None  # nothing new is authorized: the cancellation step closes the rest
        if call is None:
            halt = await _record(rt, response.data.request_event_id, use)
        elif _undecided(rt, call.data.call_id):
            state = call_state(rt.events, call.data.call_id)
            halt = await calls.authorize(rt, state, calls.pending_spec(rt, call.data.call_id))
        else:
            continue
        if halt is not None:
            return halt
    return None


async def _record(rt: Runtime, request_id: EventId, use: ToolUsePart) -> Halt | None:
    """The call, then its authorization, or, when it can't run, its error result."""
    why = _invalid(rt, use)
    drafts = [call_draft(request_id, use)]
    if why is not None:
        drafts.append(_not_executed(use, why))
    done = await rt.append(*drafts)
    if isinstance(done, Err):
        return lost(done.error)
    if why is not None:
        return None
    state = call_state(rt.events, use.call_id)
    return await calls.authorize(rt, state, calls.pending_spec(rt, use.call_id))


def _invalid(rt: Runtime, use: ToolUsePart) -> str | None:
    spec = rt.fold.tools.get(use.name)
    if spec is None:
        return f"unknown tool {use.name}"
    if still_deferred(rt.fold, spec):
        return search.not_loaded(use.name)
    if output.is_candidate(rt.fold, spec):
        return None  # validated against the pinned output schema, recorded as output_validated
    if search.is_search(rt.fold, spec):
        return search.invalid(use.input)
    return rt.tools.invalid(spec, use.input)


async def close_unrecorded(rt: Runtime) -> Halt | None:
    """Behind a cancel barrier: each part not yet recorded gets its `tool_call` and its
    not_executed result, so no `tool_use` is left without a result."""
    response = last_response(turn_events(rt.events))
    if response is None:
        return None
    drafts: list[Draft] = []
    for use, call in response_calls(rt.events):
        if call is None:
            request = response.data.request_event_id
            drafts += [call_draft(request, use), _not_executed(use, "not executed: cancelled")]
    if not drafts:
        return None
    done = await rt.append(*drafts)
    return lost(done.error) if isinstance(done, Err) else None


def _not_executed(use: ToolUsePart, why: str) -> Draft:
    """The result of a call that never ran: refused before any effect, or cancelled."""
    data: dict[str, JsonValue] = {
        "call_id": use.call_id,
        "completeness": "complete",
        "is_error": True,
        "origin": "not_executed",
        "preview": why,
    }
    return draft("tool_result", data)
