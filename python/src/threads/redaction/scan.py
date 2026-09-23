"""The matcher text and bytes redaction both run: the longest registered value at each position,
then the whole output checked again, so a marker joined to its neighbours never forms a value."""

import re
from collections.abc import Callable
from dataclasses import dataclass

from threads.redaction.registry import ordered


@dataclass(frozen=True, slots=True)
class Replacements:
    """Value to marker, in the unit `encode` gives (text, or bytes as latin-1 text)."""

    markers: dict[str, str]
    redacted: str
    """What a piece of output that still holds a value becomes."""

    def holds(self, text: str) -> bool:
        return any(v in text for v in self.markers)


def replacements(encode: Callable[[str], str]) -> Replacements:
    markers = {encode(v): encode(_marker(label)) for v, label in ordered()}
    plain = encode("[redacted]")
    return Replacements(markers, "" if any(v in plain for v in markers) else plain)


def _marker(label: str) -> str:
    """`[secret <label>]`, or `[secret]` when the label holds a registered value."""
    labelled = f"[secret {label}]"
    return "[secret]" if any(v in labelled for v, _ in ordered()) else labelled


def _held_from(pending: str, values: dict[str, str]) -> int:
    """The first position whose rest is a proper prefix of a value: it may still become one."""
    longest = max(len(v) for v in values)
    for i in range(max(0, len(pending) - longest + 1), len(pending)):
        tail = pending[i:]
        if any(len(v) > len(tail) and v.startswith(tail) for v in values):
            return i
    return len(pending)


def scan(pending: str, r: Replacements, *, final: bool) -> tuple[str, str]:
    """The longest value at each position, scanning left to right. Unless `final`, it stops
    where the rest could still grow into a longer value (`abc` before a possible `abc123`), and
    returns that rest to hold for the next chunk."""
    values = r.markers
    if not values:
        return pending, ""
    hold = len(pending) if final else _held_from(pending, values)
    pattern = re.compile("|".join(re.escape(v) for v in values))
    out: list[str] = []
    at = 0
    for m in pattern.finditer(pending):
        if m.start() >= hold:
            break
        out += [pending[at : m.start()], values[m.group(0)]]
        at = m.end()
    cut = max(hold, at)
    out.append(pending[at:cut])
    return "".join(out), pending[cut:]


class Stream:
    """A stream redacted as it arrives. A chunk that, joined to the tail already emitted, would
    hold a value is emitted as the plain marker instead."""

    def __init__(self, encode: Callable[[str], str]) -> None:
        self._encode = encode
        self._pending = ""
        self._emitted = ""

    def feed(self, text: str) -> str:
        self._pending += text
        return self._take(final=False)

    def end(self) -> str:
        return self._take(final=True)

    def _take(self, *, final: bool) -> str:
        r = replacements(self._encode)
        out, self._pending = scan(self._pending, r, final=final)
        shown = r.redacted if r.holds(self._emitted + out) else out
        # Only the last longest-1 units can join the next chunk into a value.
        keep = max((len(v) - 1 for v in r.markers), default=0)
        self._emitted = (self._emitted + shown)[-keep:] if keep else ""
        return shown
