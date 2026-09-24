"""How a rerun is compared with its recording (spec lane 22, B.2): the appended events after
normalizing what differs between two runs of the same turn (event ids and the references to them,
time, epoch, the hash chain), and the case's `must` matchers."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import JsonValue

from threads.log.jcs import canonicalize
from threads.result import Ok

type Wire = Mapping[str, JsonValue]
"""An event as its wire JSON object."""

_ENVELOPE: Final = ("type", "seq", "epoch", "branch_id", "critical")


def canonical(value: JsonValue) -> str:
    text = canonicalize(value)
    if not isinstance(text, Ok):
        raise AssertionError(f"a recorded value is canonical JSON: {text.error}")
    return text.value


def _subset(want: JsonValue, got: JsonValue) -> bool:
    if isinstance(want, dict) and isinstance(got, dict):
        return all(k in got and _subset(v, got[k]) for k, v in want.items())
    if isinstance(want, dict) or isinstance(got, dict):
        return False
    return canonical(want) == canonical(got)


def matches(matcher: Wire, event: Wire) -> bool:
    """A matcher against one event: listed envelope keys exactly, data as a deep subset."""
    for key in _ENVELOPE:
        if key in matcher and matcher[key] != event.get(key):
            return False
    actor = event.get("actor")
    kind = actor.get("kind") if isinstance(actor, dict) else None
    if "actor_kind" in matcher and matcher["actor_kind"] != kind:
        return False
    return "data" not in matcher or _subset(matcher["data"], event.get("data"))


_TIMES: Final = frozenset({"not_before", "expires_at", "deadline", "scheduled_for"})
"""Data fields that hold a time: compared as an offset from the event's own time."""


def _relabel(value: JsonValue, ids: Mapping[str, str], time: int, key: str = "") -> JsonValue:
    if isinstance(value, str):
        return ids.get(value, value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and key in _TIMES:
        return value - time
    if isinstance(value, list):
        return [_relabel(v, ids, time) for v in value]
    if isinstance(value, dict):
        return {k: _relabel(v, ids, time, k) for k, v in value.items()}
    return value


def _int(value: JsonValue) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def normalized(events: Sequence[Wire]) -> list[str]:
    """Each event as the comparison sees it: its event id, and every reference to an id of the
    list, becomes its position; time, epoch and prev_hash are dropped."""
    ids = {str(e.get("event_id")): f"#{i}" for i, e in enumerate(events)}
    out: list[str] = []
    for e in events:
        time = _int(e.get("time"))
        out.append(
            canonical(
                {
                    "seq": e.get("seq"),
                    "type": e.get("type"),
                    "type_version": e.get("type_version"),
                    "critical": e.get("critical"),
                    "actor": _relabel(e.get("actor"), ids, time),
                    "data": _relabel(e.get("data"), ids, time),
                }
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class Mismatch:
    index: int
    want: str | None
    got: str | None
    hint: str | None = None


def _type(events: Sequence[Wire], index: int) -> str | None:
    if index >= len(events):
        return None
    kind = events[index].get("type")
    return kind if isinstance(kind, str) else None


def first_mismatch(recorded: Sequence[Wire], rerun: Sequence[Wire]) -> Mismatch | None:
    """The first appended event that differs from the recording, if any."""
    want, got = normalized(recorded), normalized(rerun)
    for index in range(max(len(want), len(got))):
        if index >= len(want) or index >= len(got) or want[index] != got[index]:
            return Mismatch(index, _type(recorded, index), _type(rerun, index))
    return None


def matcher_mismatch(want: Sequence[Wire], got: Sequence[Wire]) -> Mismatch | None:
    """An older case's `appended` matchers: exact count and order, each matching its event."""
    for index in range(max(len(want), len(got))):
        if index >= len(want) or index >= len(got) or not matches(want[index], got[index]):
            m = want[index].get("type") if index < len(want) else None
            return Mismatch(index, m if isinstance(m, str) else None, _type(got, index))
    return None


def first_line(data: bytes) -> bytes:
    """Render v1 line 0 of a request artifact: its bytes up to the first newline."""
    end = data.find(b"\n")
    return data if end == -1 else data[:end]
