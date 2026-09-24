"""extensions.json (spec lane 22, A.2): the turn's hook decisions and recall with their full
outcomes, which the offline rerun replays through stand-in extensions, since it loads no user code.
Observation records are keyed by the call they belong to."""

from collections.abc import Mapping, Sequence
from typing import Final

from pydantic import JsonValue

from threads.evals.kinds import HOOK_KINDS
from threads.log import ContextEditedEvent, Event, HookDecisionEvent, InjectedEvent, ToolResultEvent
from threads.reduce.handlers import to_json

_TRIGGERS: Final[Mapping[str, frozenset[str]]] = {
    "after_model_switch": frozenset({"settings_changed"}),
    "notification": frozenset({"parked", "retry_scheduled", "budget_exceeded", "agent_finished"}),
}
"""What sets off each keyed observation hook, counted within the turn."""


def _key(turn: Sequence[Event], at: int, d: HookDecisionEvent) -> JsonValue | None:
    """The call an observation record belongs to, from events the rerun reproduces."""
    if HOOK_KINDS[d.data.hook] == "recorded":
        return None
    call_id = d.data.call_id
    if isinstance(call_id, str):
        return {"call_id": call_id}
    trigger = _TRIGGERS.get(d.data.hook)
    if trigger is None:
        return {"occurrence": 1}
    count = sum(1 for e in turn[:at] if e.type in trigger)
    return {"occurrence": max(count, 1)}


def _own(e: Event | None, extension: str) -> bool:
    return (
        isinstance(e, InjectedEvent) and e.data.source == "hook" and e.data.origin.id == extension
    )


def _injected(turn: Sequence[Event], at: int, d: HookDecisionEvent) -> list[JsonValue]:
    """The injected{source: hook} events this decision produced: its own append, and a guide's
    text (after_model's guide is appended after the withheld calls' results)."""
    ext = d.data.extension
    out: list[JsonValue] = []
    i = at + 1
    while i < len(turn) and _own(turn[i], ext):
        e = turn[i]
        if isinstance(e, InjectedEvent):
            out.append(to_json(e.data))
        i += 1
    if out or d.data.decision != "guide":
        return out
    guide = next(
        (
            e
            for e in turn[at + 1 :]
            if isinstance(e, InjectedEvent) and _own(e, ext) and e.data.text == d.data.reason
        ),
        None,
    )
    return [] if guide is None else [to_json(guide.data)]


def _spans(turn: Sequence[Event], at: int) -> JsonValue | None:
    """before_tool_result redact: the spans its context_edited carries."""
    after = turn[at + 1] if at + 1 < len(turn) else None
    if not isinstance(after, ContextEditedEvent) or not after.data.edits:
        return None
    edit = to_json(after.data.edits[0])
    return edit.get("spans") if isinstance(edit, dict) and edit.get("action") == "redact" else None


def _hooks(turn: Sequence[Event]) -> list[JsonValue]:
    seen: dict[tuple[str, str], int] = {}
    records: list[JsonValue] = []
    for i, e in enumerate(turn):
        if not isinstance(e, HookDecisionEvent):
            continue
        d = e.data
        seen[(d.extension, d.hook)] = seen.get((d.extension, d.hook), 0) + 1
        record: dict[str, JsonValue] = {
            "extension": d.extension,
            "hook": d.hook,
            "occurrence": seen[(d.extension, d.hook)],
        }
        at = _key(turn, i, e)
        if at is not None:
            record["at"] = at
        record["decision"] = d.decision
        if isinstance(d.reason, str):
            record["reason"] = d.reason
        record["injected"] = _injected(turn, i, e)
        spans = _spans(turn, i) if d.decision == "redact" else None
        if spans is not None:
            record["spans"] = spans
        records.append(record)
    return records


def _recall(turn: Sequence[Event]) -> list[JsonValue]:
    """Each recalling call's injected items: the memory or knowledge a search brought back."""
    seen: dict[str, int] = {}
    records: list[JsonValue] = []
    open_items: list[JsonValue] | None = None
    open_source = ""
    for e in turn:
        if isinstance(e, ToolResultEvent):
            open_items = None
        if not isinstance(e, InjectedEvent) or e.data.source not in ("memory", "knowledge"):
            continue
        source = e.data.source
        if open_items is None or open_source != source:
            seen[source] = seen.get(source, 0) + 1
            open_items, open_source = [], source
            records.append({"source": source, "occurrence": seen[source], "items": open_items})
        open_items.append(to_json(e.data))
    return records


def extension_script(turn: Sequence[Event]) -> JsonValue | None:
    """The turn's extensions.json, or None when no hook decided and nothing was recalled."""
    hooks, recall = _hooks(turn), _recall(turn)
    return {"hooks": hooks, "recall": recall} if hooks or recall else None
