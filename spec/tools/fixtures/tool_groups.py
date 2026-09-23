# pyright: strict
"""The parallel tool call vector (spec/schema/README.md, Parallel tool calls): pending calls in
call order and the dispatch plan both runtimes' `groups` must return. A plan is a list of groups
of call indexes; a group of one runs alone. The plans are authored from the rule, never read from
an implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import CASES
from .pieces import dump

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

VECTOR = CASES.parent / "vectors" / "tool-groups.json"


def _call(name: str, **changes: JsonValue) -> Obj:
    """A concurrent, allowed read_only app tool call, with `changes`."""
    base: Obj = {
        "name": name,
        "effect_class": "read_only",
        "concurrent": True,
        "framework": False,
        "ends_turn": False,
        "decision": "allow",
    }
    return {**base, **changes}


READ = _call("search")
PLAIN = _call("lookup", concurrent=False)
WRITE = _call("send_email", effect_class="unguarded", concurrent=False)

# (name, pending calls, plan)
CASES_: tuple[tuple[str, list[JsonValue], list[JsonValue]], ...] = (
    ("two concurrent reads", [READ, READ], [[0, 1]]),
    ("a single concurrent read", [READ], [[0]]),
    ("read, write, read", [READ, WRITE, READ], [[0], [1], [2]]),
    ("two groups around a write", [READ, READ, WRITE, READ, READ], [[0, 1], [2], [3, 4]]),
    ("a non-concurrent read is a barrier", [READ, PLAIN, READ], [[0], [1], [2]]),
    ("an ask ends the group", [READ, _call("search", decision="ask")], [[0], [1]]),
    (
        "an approved ask stays a barrier: its recorded decision is still ask",
        [_call("search", decision="ask"), READ, READ],
        [[0], [1, 2]],
    ),
    (
        "a deny in the middle",
        [READ, _call("search", decision="deny"), READ],
        [[0], [1], [2]],
    ),
    (
        "an unauthorized call ends the group",
        [READ, _call("search", decision="none"), READ],
        [[0], [1], [2]],
    ),
    (
        "a framework tool between reads",
        [READ, _call("todo_write", concurrent=False, framework=True), READ],
        [[0], [1], [2]],
    ),
    (
        "a concurrent flag on a framework tool is ignored",
        [READ, _call("todo_write", framework=True), READ],
        [[0], [1], [2]],
    ),
    (
        "a pinned effect other than read_only is a barrier",
        [READ, _call("search", effect_class="idempotent"), READ],
        [[0], [1], [2]],
    ),
    (
        "a tool that ends the turn runs alone",
        [READ, _call("finish", ends_turn=True), READ],
        [[0], [1], [2]],
    ),
    ("three concurrent reads", [READ, READ, READ], [[0, 1, 2]]),
)


RECORDED_ORDER: list[JsonValue] = [
    "tool_result:call_1",
    "after_tool:call_1",
    "tool_result:call_2",
    "injected",
    "after_tool:call_2",
    "tool_result:call_3",
    "after_tool:call_3",
]


def _vector() -> str:
    cases: list[JsonValue] = [
        {"name": n, "pending": pending, "plan": plan} for n, pending, plan in CASES_
    ]
    doc: Obj = {
        "description": (
            "Parallel tool calls (spec/schema/README.md): each case's pending calls, in call "
            "order, and the dispatch plan groups() must return. A call joins a group only when "
            "its tool is concurrent, read_only, not a framework tool, doesn't end the turn, and "
            "its decision is allow; the group is the longest such run from the first pending "
            "call, and every other call runs alone. decision none: not authorized yet. "
            "recorded_order: three concurrent reads a, b, c that finish c, b, a, where b brings "
            "an injection and an extension observes after_tool; both runtimes record exactly this "
            "sequence of tool_result, injected and after_tool hook_decision events."
        ),
        "cases": cases,
        "recorded_order": RECORDED_ORDER,
    }
    return dump(doc)


def write() -> None:
    VECTOR.write_text(_vector(), encoding="utf-8")


def check() -> list[str]:
    fresh = _vector()
    current = VECTOR.read_text(encoding="utf-8") if VECTOR.exists() else ""
    return [] if fresh == current else [f"{VECTOR.name}: differs; run gen_fixtures.py"]
