"""handoff: the conversation moves to a new thread with its own pinned line 0
and policy, so C7 holds per thread. The target keeps the originating principal and gets the
forwarded transcript as untrusted reference plus the pending request; the old thread takes no
input after it (semantic rule 26)."""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.tools_v1 import HandoffInput
from threads.agents.bindings import permissions
from threads.agents.definition import Definition
from threads.agents.launch import Launch
from threads.agents.scope import Scope
from threads.log import (
    Event,
    HandoffEvent,
    ModelResponseEvent,
    ModelResponseRecoveredEvent,
    TextPart,
    UserInputEvent,
)
from threads.loop.budget import inherited
from threads.loop.drafts import draft
from threads.loop.history import CallState
from threads.loop.results import As, result_draft, text_ref
from threads.loop.runtime import Halt, Runtime, lost
from threads.reduce.fold import policy
from threads.result import Err
from threads.store.lines import uuid7

if TYPE_CHECKING:
    from pydantic import JsonValue


async def handoff[D](scope: Scope[D], rt: Runtime, state: CallState) -> Halt | None:
    """A target the pinned policy.handoffs doesn't list fails pre-effect."""
    call_id = state.call.data.call_id
    name = HandoffInput.model_validate(dict(state.call.data.input)).agent
    pinned = policy(rt.fold)
    listed = [] if pinned is None or pinned.handoffs is MISSING else list(pinned.handoffs)
    if name not in listed or target(scope, name) is None:
        why = f"{name} is not a handoff target of this agent"
        done = await rt.append(await result_draft(rt, call_id, why, As("not_executed", True)))
        return lost(done.error) if isinstance(done, Err) else None
    data: dict[str, JsonValue] = {
        "call_id": call_id,
        "to_agent": name,
        "to_thread_id": uuid7(rt.clock()),
        "forwarded": "transcript",
        "forwarded_ref": await text_ref(rt, _transcript(rt.events)),
    }
    result = await result_draft(rt, call_id, f"handed off to {name}", As("executed"))
    done = await rt.append(
        draft("handoff", data), result, draft("turn_completed", {"reason": "handoff"})
    )
    return lost(done.error) if isinstance(done, Err) else None


def target[D](scope: Scope[D], name: str) -> Definition[None] | None:
    return next((d for d in scope.definition.handoffs if d.name == name), None)


def _transcript(events: Sequence[Event]) -> str:
    """The conversation so far as plain text: what the user asked and the agent answered."""
    lines: list[str] = []
    for event in events:
        if isinstance(event, UserInputEvent) and isinstance(event.data.text, str):
            lines.append(f"user: {event.data.text}")
        elif isinstance(event, ModelResponseEvent | ModelResponseRecoveredEvent):
            said = "".join(p.text for p in event.data.content if isinstance(p, TextPart))
            if said:
                lines.append(f"assistant: {said}")
    return "\n".join(lines)


def launch[D](scope: Scope[D], rt: Runtime, event: HandoffEvent) -> tuple[Launch, str]:
    """The target's launch and the pending request it answers: under the originating principal,
    the current ceilings and every budget covering the source, never the source agent's own
    tools."""
    thread_id = rt.fold.thread_id
    if thread_id is None:
        raise AssertionError("an acquired branch has a thread")
    parent: dict[str, JsonValue] = {
        "thread_id": thread_id,
        "branch_id": rt.writer.branch_id,
        "event_id": event.event_id,
        "relation": "handoff",
    }
    request = next(e for e in reversed(rt.events) if isinstance(e, UserInputEvent))
    forwarded: dict[str, JsonValue] = {
        "source": "handoff",
        "trust": "untrusted_reference",
        "origin": {"id": thread_id},
    }
    if event.data.forwarded_ref is not MISSING:
        forwarded["ref"] = event.data.forwarded_ref.model_dump(mode="json")
    who = request.actor.principal
    ceilings = scope.ceilings
    if scope.definition.member:
        # A subagent's target never runs under fewer ceilings than the subagent itself.
        ceilings = (permissions(rt.fold).model_copy(update={"mode": rt.fold.mode}), *ceilings)
    text = request.data.text if isinstance(request.data.text, str) else ""
    chosen = Launch(
        event.data.to_thread_id,
        parent,
        "handoff",
        who,
        1,
        budgets=tuple(inherited(thread_id, rt.fold, rt.budgets)),
        ceilings=ceilings,
        before_input=(draft("injected", forwarded),),
    )
    return chosen, text
