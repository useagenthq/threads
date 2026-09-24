"""Structured final output in tool mode: each final_output candidate is checked once and recorded
as output_validated next to its raw tool_call. A rejected candidate and a turn that ends in plain
text both count as failed candidates; after `max_retries` of them the turn ends output_invalid."""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from pydantic.experimental.missing_sentinel import MISSING

from threads._json_schema import conforms
from threads._tool_names import FINAL_OUTPUT
from threads.log import Event, InjectedEvent, OutputValidatedEvent, ToolSpec
from threads.log import Output as OutputPolicy
from threads.log.jcs import canonicalize
from threads.loop import gates
from threads.loop.drafts import draft
from threads.loop.history import CallState, turn_events
from threads.loop.results import As, result_draft
from threads.loop.runtime import Halt, Runtime, lost
from threads.reduce.fold import Fold, policy
from threads.result import Err, Ok
from threads.store import Draft

if TYPE_CHECKING:
    from pydantic import JsonValue

ASK: Final = f"Return the final result by calling {FINAL_OUTPUT}."


def pinned(fold: Fold) -> OutputPolicy | None:
    """The pinned `policy.output`, or None when the agent has no output schema."""
    pinned_policy = policy(fold)
    if pinned_policy is None or pinned_policy.output is MISSING:
        return None
    return pinned_policy.output


def is_candidate(fold: Fold, spec: ToolSpec) -> bool:
    """A final_output call under a pinned output schema: validated, never dispatched."""
    return spec.name == FINAL_OUTPUT and pinned(fold) is not None


async def validate(rt: Runtime, state: CallState) -> Halt | None:
    """Checks the candidate through the agent's output binding (without one in this process it
    is rejected: fail closed). Accepted: its result previews the value as canonical JSON.
    Rejected: an error result, and the model tries again unless this was the last try."""
    output = pinned(rt.fold)
    if output is None:
        raise AssertionError("final_output is validated only under policy.output")
    value: JsonValue = dict(state.call.data.input)
    why = "unsupported: no output binding" if rt.output is None else rt.output(value)
    if why is None and not conforms(output.schema_, value):
        # The log's own check (semantic rule 20) is what an accepted value must pass, whatever
        # the agent's validator lets through (a naive datetime, a URL without a scheme).
        why = "the value fails the pinned output schema (its formats and bounds)"
    data: dict[str, JsonValue] = {
        "source_event_id": state.call.event_id,
        "schema_sha256": output.schema_sha256,
        "outcome": "accepted" if why is None else "rejected",
    }
    call_id = state.call.data.call_id
    if why is None:
        data["value"] = value
        shown = canonicalize(value)
        preview = shown.value if isinstance(shown, Ok) else ""
        result = await result_draft(rt, call_id, preview, As("executed"))
        return await _append(rt, [draft("output_validated", data), result], give_up=False)
    data["errors"] = [{"path": "", "message": why}]
    text = f"{FINAL_OUTPUT} rejected: {why}"
    result = await result_draft(rt, call_id, text, As("not_executed", True))
    drafts = [draft("output_validated", data), result]
    return await _append(rt, drafts, give_up=_last_try(rt.events, output))


async def ask(rt: Runtime) -> Halt | None:
    """A turn that ended in plain text while an output schema is pinned: ask for final_output,
    or end the turn output_invalid when that was the last try."""
    output = pinned(rt.fold)
    if output is None:
        raise AssertionError("final_output is asked for only under policy.output")
    data: dict[str, JsonValue] = {
        "source": "recovery",
        "trust": "trusted_instruction",
        "origin": {"id": FINAL_OUTPUT},
        "text": ASK,
    }
    return await _append(rt, [draft("injected", data)], give_up=_last_try(rt.events, output))


def _last_try(events: Sequence[Event], output: OutputPolicy) -> bool:
    """Whether the failed candidate being recorded is the `max_retries`-th of the turn."""
    turn = turn_events(events)
    rejected = sum(
        1 for e in turn if isinstance(e, OutputValidatedEvent) and e.data.outcome == "rejected"
    )
    asked = sum(
        1 for e in turn if isinstance(e, InjectedEvent) and e.data.origin.id == FINAL_OUTPUT
    )
    return rejected + asked + 1 >= output.max_retries


async def _append(rt: Runtime, drafts: list[Draft], *, give_up: bool) -> Halt | None:
    if give_up:
        drafts.append(draft("turn_completed", {"reason": "output_invalid"}))
    done = await rt.append(*drafts)
    if isinstance(done, Err):
        return lost(done.error)
    return await gates.failed(rt, "output_invalid") if give_up else None
