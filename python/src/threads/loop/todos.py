"""The agent's todo list: `todo_write` replaces it with `todos_updated`, and a
reminder shows open items to a turn after ten turns without a `todo_write`."""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from threads._generated.tools_v1 import TodoWriteInput
from threads.log import Event, InjectedEvent, ModelRequestEvent, TodosUpdatedEvent, UserInputEvent
from threads.loop.drafts import draft
from threads.loop.gates import AGAIN, Gated
from threads.loop.history import CallState, turn_events
from threads.loop.results import As, result_draft
from threads.loop.runtime import Halt, Runtime, lost
from threads.result import Err

if TYPE_CHECKING:
    from pydantic import JsonValue

REMIND_AFTER: Final = 10
"""Turns without a todo_write (and since the last reminder) before open items are shown."""
_ORIGIN: Final = "todos"


async def write(rt: Runtime, state: CallState) -> Halt | None:
    """The whole list, validated at the call boundary; a list whose ids repeat is an error
    result and the prior list stands (semantic rule 24)."""
    call_id = state.call.data.call_id
    todos = TodoWriteInput.model_validate(dict(state.call.data.input)).todos
    ids = [t.id for t in todos]
    if len(set(ids)) != len(ids):
        why = "todo ids must be unique; the list is unchanged"
        result = await result_draft(rt, call_id, why, As("not_executed", True))
        done = await rt.append(result)
    else:
        items: list[JsonValue] = [t.model_dump(mode="json") for t in todos]
        data: dict[str, JsonValue] = {"call_id": call_id, "todos": items}
        result = await result_draft(rt, call_id, "ok", As("executed"))
        done = await rt.append(draft("todos_updated", data), result)
    return lost(done.error) if isinstance(done, Err) else None


async def remind(rt: Runtime) -> Gated:
    """Right after the turn's input, before its first request: open items, when ten turns
    passed since the last todo_write or reminder (F8.4)."""
    turn = turn_events(rt.events)
    if any(isinstance(e, ModelRequestEvent) for e in turn):
        return None
    latest = _latest(rt.events)
    if latest is None:
        return None
    lines = [f"- [{t.status}] {t.content}" for t in latest.data.todos if t.status != "completed"]
    if not lines or _turns_since(rt.events) < REMIND_AFTER:
        return None
    data: dict[str, JsonValue] = {
        "source": "todo",
        "trust": "untrusted_reference",
        "origin": {"id": _ORIGIN},
        "text": "\n".join(lines),
    }
    done = await rt.append(draft("injected", data))
    return lost(done.error) if isinstance(done, Err) else AGAIN


def _latest(events: Sequence[Event]) -> TodosUpdatedEvent | None:
    return next((e for e in reversed(events) if isinstance(e, TodosUpdatedEvent)), None)


def _turns_since(events: Sequence[Event]) -> int:
    """Earlier turns started after the last todo_write or reminder (the current one excluded)."""
    since = 0
    for index, event in enumerate(events):
        reminder = isinstance(event, InjectedEvent) and event.data.source == "todo"
        if reminder or isinstance(event, TodosUpdatedEvent):
            since = index
    return sum(1 for e in events[since:] if isinstance(e, UserInputEvent)) - 1
