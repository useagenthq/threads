# pyright: strict
"""The handoff transcript vector (spec/conformance/vectors/handoff-transcripts.json;
spec/schema/README.md, "Handoff scope"): the text a handoff forwards to its target, from the
source log's event lines. The expected text comes from the reference below, written from the
spec's rules, never read from an implementation. Both runtimes parse each line with their line
parser and must produce the same text."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import CASES, arr, obj, text
from .jcs import canonical
from .log import Log
from .pieces import answer, call, dump, result, started, user
from .teams import catalog_specs

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

VECTOR = CASES.parent / "vectors" / "handoff-transcripts.json"
DEFAULT_CAP = 32768


def _text(content: list[JsonValue]) -> str:
    return "".join(text(obj(p)["text"]) for p in content if obj(p)["type"] == "text")


def _tool_line(e: Obj) -> str | None:
    d = obj(e["data"])
    if e["type"] == "tool_call" and d["name"] != "handoff":
        return f"tool_call {text(d['call_id'])} {text(d['name'])}: {canonical(d['input']).decode()}"
    if e["type"] in ("tool_result", "tool_result_late"):
        flag = " (error)" if d["is_error"] is True else ""
        return f"tool_result {text(d['call_id'])}{flag}: {text(d['preview'])}"
    return None


def transcript(events: list[Obj], cap: int) -> str:
    """The reference: user and assistant lines of the whole log, then the handing-off turn's
    tool lines in place, the oldest dropped while they exceed `cap` UTF-8 bytes."""
    opener = max(
        (i for i, e in enumerate(events) if e["type"] in ("user_input", "woken")), default=-1
    )
    lines: list[str] = []
    tools: list[int] = []  # positions in `lines` of tool lines
    for i, e in enumerate(events):
        d = obj(e["data"])
        if e["type"] == "user_input" and isinstance(d.get("text"), str):
            lines.append(f"user: {text(d['text'])}")
        elif e["type"] in ("model_response", "model_response_recovered"):
            said = _text(arr(d["content"]))
            if said:
                lines.append(f"assistant: {said}")
        elif i > opener and (line := _tool_line(e)) is not None:
            tools.append(len(lines))
            lines.append(line)
    size = sum(len(lines[i].encode()) for i in tools) + max(len(tools) - 1, 0)
    dropped: list[int] = []
    while tools and size > cap:
        first = tools.pop(0)
        size -= len(lines[first].encode()) + (1 if tools else 0)
        dropped.append(first)
    if dropped:
        marker = f"[{len(dropped)} earlier tool lines dropped]"
        lines = [
            marker if i == dropped[0] else s for i, s in enumerate(lines) if i not in dropped[1:]
        ]
    return "\n".join(lines)


def _base() -> Log:
    log = Log()
    started(log, catalog_specs(("handoff",)))
    user(log, "Where is order 42?")
    call(log, "lookup_order", {"id": 41}, "call_0")
    result(log, "call_0", "order 41 shipped")
    answer(log, "Which order do you mean?")
    user(log, "Order 42, and refund it.")
    call(log, "lookup_order", {"id": 42}, "call_1")
    result(log, "call_1", "order 42 shipped on 2026-09-20")
    return log


def _cases() -> list[tuple[str, Log, int]]:
    handoff: Obj = {"agent": "billing"}
    plain = _base()
    call(plain, "handoff", handoff, "call_2")
    errors = _base()
    call(errors, "refund", {"order": 42}, "call_2")
    result(errors, "call_2", "refunds need billing", is_error=True)
    errors.add(
        "tool_result_late",
        {"call_id": "call_0", "is_error": False, "completeness": "complete", "preview": "late"},
        actor="tool",
    )
    call(errors, "handoff", handoff, "call_3")
    capped = _base()
    call(capped, "refund", {"order": 42}, "call_2")
    result(capped, "call_2", "refunds need billing", is_error=True)
    call(capped, "handoff", handoff, "call_3")
    return [
        ("tool-context-of-the-handing-off-turn", plain, DEFAULT_CAP),
        ("errors-and-late-results", errors, DEFAULT_CAP),
        ("oldest-tool-lines-dropped-over-the-cap", capped, 80),
    ]


def _vector() -> str:
    cases: list[JsonValue] = []
    for name, log, cap in _cases():
        lines: list[JsonValue] = [canonical(e).decode() for e in log.events]
        cases.append(
            {"name": name, "cap": cap, "lines": lines, "transcript": transcript(log.events, cap)}
        )
    doc: Obj = {
        "description": (
            "Handoff transcripts (spec/schema/README.md, Handoff scope): parse each event line, "
            "then build the forwarded transcript with the tool lines capped at `cap` bytes; it "
            "must equal `transcript`."
        ),
        "cases": cases,
    }
    return dump(doc)


def write() -> None:
    VECTOR.write_text(_vector(), encoding="utf-8")


def check() -> list[str]:
    fresh = _vector()
    current = VECTOR.read_text(encoding="utf-8") if VECTOR.exists() else ""
    return [] if fresh == current else [f"{VECTOR.name}: differs; run gen_fixtures.py"]
