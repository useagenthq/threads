"""How a team thread's turn end settles it (design §2.4, the outcome map): end_turn leaves the
member idle with its answer; every other end ends it. A lead's handoff is handed_off; a member
can't hand off (setup refuses it: handoff_in_team)."""

from collections.abc import Mapping, Sequence

from pydantic import JsonValue, TypeAdapter

from threads.log import Event
from threads.log.jcs import canonicalize
from threads.loop.runtime import FAILED_CODES, FAILED_MESSAGES
from threads.reduce.handlers import to_json
from threads.result import Ok
from threads.store.lines import Draft
from threads.team.settle import Completed, Settlement

type _Item = tuple[str, Mapping[str, JsonValue]]
_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


def _items(turn: Sequence[Event], batch: Sequence[Draft]) -> list[_Item]:
    items: list[_Item] = [(e.type, _OBJECT.validate_python(to_json(e.data))) for e in turn]
    return items + [(d.type, d.data) for d in batch]


def _last(items: Sequence[_Item], kind: str) -> Mapping[str, JsonValue] | None:
    return next((data for t, data in reversed(items) if t == kind), None)


def settlement_of(turn: Sequence[Event], batch: Sequence[Draft]) -> Settlement | None:
    """The settlement of the turn `batch` ends, read from the turn's events and the batch."""
    items = _items(turn, batch)
    end = next((d.data for d in reversed(batch) if d.type == "turn_completed"), None)
    if end is None:
        return None
    reason = end["reason"]
    if reason == "end_turn":
        return Completed(_answer(items))
    if reason == "cancelled":
        return {"status": "cancelled"}
    if reason == "budget_exhausted":
        why = _last(items, "budget_exceeded")
        if why is None:
            raise AssertionError("budget_exhausted records why")
        return {"status": "budget_exhausted", "budget": dict(why)}
    if reason == "handoff":
        handoff = _last(items, "handoff")
        if handoff is None:
            raise AssertionError("a handoff's turn records its handoff")
        return {"status": "handed_off", "to_thread": handoff["to_thread_id"]}
    return {"status": "failed", "error": _failure(str(reason), end.get("code"))}


def _failure(reason: str, code: JsonValue) -> JsonValue:
    """A failed end's code and message: the same as a run's RunResult.failed."""
    if code in ("pin_unavailable", "pin_mismatch"):
        raise AssertionError(f"a run's turn can't end {code}: only a member rebinds")
    message = FAILED_MESSAGES.get(reason, FAILED_MESSAGES["error"])
    found = code if isinstance(code, str) else FAILED_CODES.get(reason)
    if found is None:
        raise AssertionError(f"{reason} is a failed end")
    return {"code": found, "message": message}


def _answer(items: Sequence[_Item]) -> str:
    """The turn's answer: its accepted structured value as RFC 8785 JSON, else its last text."""
    accepted = next(
        (d for t, d in reversed(items) if t == "output_validated" and d["outcome"] == "accepted"),
        None,
    )
    if accepted is not None and "value" in accepted:
        text = canonicalize(accepted["value"])
        if isinstance(text, Ok):
            return text.value
    said = next(
        (d for t, d in reversed(items) if t in ("model_response", "model_response_recovered")),
        None,
    )
    content = [] if said is None else said.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(
        str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text"
    )
