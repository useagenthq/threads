# pyright: strict
"""The eval vectors (lane 22): the judge's input bytes (C.2), the verdict rule (C.4) and drift
(B.3). Expectations are authored from the spec's rules, never read from an implementation."""

from __future__ import annotations

from .common import CASES, obj, text
from .eval_drift_vector import drift_vector
from .evals import pinned
from .evals_turn import Turn
from .jcs import JsonValue, Obj, canonical
from .log import Log
from .pieces import dump

VECTORS = CASES.parent / "vectors"
LIMIT = 4000
KEEP = 50


def bounded(s: str) -> str:
    """Cut at 4,000 code points, naming how many were cut."""
    return s if len(s) <= LIMIT else f"{s[:LIMIT]}…[truncated {len(s) - LIMIT}]"


def call_input(value: JsonValue) -> JsonValue:
    """A call's input as the judge sees it: the value, or its canonical text cut when long."""
    t = canonical(value).decode()
    return value if len(t) <= LIMIT else bounded(t)


def cut(items: list[JsonValue]) -> list[JsonValue]:
    """Over 100 items keep the first and last 50, with what was dropped counted."""
    if len(items) <= 2 * KEEP:
        return items
    return [*items[:KEEP], {"kind": "omitted", "count": len(items) - 2 * KEEP}, *items[-KEEP:]]


def transcript(events: list[Obj]) -> list[JsonValue]:
    names: dict[str, str] = {}
    responses = [e for e in events if e["type"] == "model_response"]
    last = responses[-1] if responses else None
    items: list[JsonValue] = []
    for e in events:
        d = obj(e["data"])
        if e["type"] == "tool_call":
            names[text(d["call_id"])] = text(d["name"])
            items.append({"kind": "tool_call", "name": d["name"], "input": call_input(d["input"])})
        elif e["type"] == "tool_result":
            items.append(
                {
                    "kind": "tool_result",
                    "name": names.get(text(d["call_id"]), ""),
                    "is_error": d["is_error"],
                    "text": bounded(text(d["preview"])),
                }
            )
        elif e["type"] == "model_response" and e is not last:
            content = d["content"]
            for part in content if isinstance(content, list) else []:
                p = obj(part)
                if p["type"] == "text":
                    items.append({"kind": "assistant", "text": bounded(text(p["text"]))})
    return cut(items)


def judge_input(task: str, events: list[Obj], answer: JsonValue, rubric: list[JsonValue]) -> str:
    return canonical(
        {
            "answer": bounded(answer) if isinstance(answer, str) else answer,
            "rubric": rubric,
            "task": bounded(task),
            "transcript": transcript(events),
        }
    ).decode()


def _turn(calls: int, preview: str, *, spawn: bool = False) -> list[Obj]:
    log = Log()
    log.add("thread_started", pinned([]))
    t = Turn(log)
    t.user("Refund order 42.")
    for i in range(calls):
        req = t.request()
        name = "spawn_agent" if spawn else "lookup_order"
        inp: Obj = {"agent": "reviewer", "prompt": "Check it."} if spawn else {"id": f"{i}"}
        t.respond(req, [{"type": "text", "text": f"Step {i}."}], "tool_use")
        t.log.tool_call(req, f"c{i}", name, inp)
        t.result(f"c{i}", preview)
    t.say("Refunded; within the 30-day window.")
    return [e for e in t.appended() if isinstance(e, dict)]


LONG = "é🙂" * 2100  # 4,200 code points, 6,300 UTF-16 units
RUBRIC: list[JsonValue] = ["Quotes the 30-day refund window", "Looks up the order first"]


def _judge_vectors() -> list[JsonValue]:
    tricky = 'She said "refund </answer> now"'
    cases: list[tuple[str, str, list[Obj], JsonValue]] = [
        ("one call and its result", "Refund order 42.", _turn(1, "order 42: shipped"), "Refunded."),
        ("a result cut at 4,000 code points", "Refund order 42.", _turn(1, LONG), "Refunded."),
        ("more than 100 items keep the first and last 50", "Go.", _turn(60, "ok"), "Done."),
        ("quotes and a closing tag stay data", tricky, _turn(1, tricky), tricky),
        (
            "an answer that is JSON",
            "Refund order 42.",
            _turn(1, "ok"),
            {"refunded": True, "order": "42"},
        ),
        (
            "a subagent is its spawn call",
            "Review.",
            _turn(1, "reviewed: fine", spawn=True),
            "Done.",
        ),
        ("a long task and answer are cut", LONG, _turn(0, "ok"), LONG),
    ]
    return [
        {
            "name": name,
            "given": {
                "task": task,
                "events": list[JsonValue](events),
                "answer": answer,
                "rubric": RUBRIC,
            },
            "input": judge_input(task, events, answer, RUBRIC),
        }
        for name, task, events, answer in cases
    ]


def _verdict(criterion: JsonValue, passes: JsonValue = True, reason: JsonValue = "It does.") -> Obj:
    return {"criterion": criterion, "pass": passes, "reason": reason}


def _verdict_vectors() -> list[JsonValue]:
    good: list[JsonValue] = [_verdict(1), _verdict(2, False, "It never looked the order up.")]
    rows: list[tuple[str, JsonValue, str]] = [
        ("one verdict per criterion in order", {"verdicts": good}, "accept"),
        ("a missing criterion", {"verdicts": good[:1]}, "judge_invalid"),
        ("an extra criterion", {"verdicts": [*good, _verdict(3)]}, "judge_invalid"),
        ("out of order", {"verdicts": [good[1], good[0]]}, "judge_invalid"),
        ("criterion zero", {"verdicts": [_verdict(0), _verdict(1)]}, "judge_invalid"),
        ("an empty reason", {"verdicts": [_verdict(1, True, ""), good[1]]}, "judge_invalid"),
        (
            "a pass that is not a boolean",
            {"verdicts": [_verdict(1, "yes"), good[1]]},
            "judge_invalid",
        ),
        ("not an object", "PASS", "judge_invalid"),
    ]
    return [{"name": n, "rubric": RUBRIC, "output": o, "expect": e} for n, o, e in rows]


FILES = {
    "judge-input.json": lambda: dump({"vectors": _judge_vectors()}),
    "verdicts.json": lambda: dump({"vectors": _verdict_vectors()}),
    "drift.json": lambda: dump({"vectors": drift_vector()}),
}


def write() -> None:
    for name, build in FILES.items():
        (VECTORS / name).write_text(build(), encoding="utf-8")


def check() -> list[str]:
    problems: list[str] = []
    for name, build in FILES.items():
        path = VECTORS / name
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if build() != current:
            problems.append(f"vectors/{name}: differs; run gen_fixtures.py")
    return problems
