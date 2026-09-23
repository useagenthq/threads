# pyright: strict
"""Structured output in tool mode (ADR 0012 5a): what counts as a failed candidate, how a
nested output schema is checked, and what a structured subagent reports to its parent."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import NOW, sha, tokens
from .jcs import JsonValue, Obj, canonical
from .log import Log, reduce
from .pieces import answer, call, case, reduce_case, reject, result, started, user, write_case
from .policies import FINAL_OUTPUT, OUTPUT, policy
from .projections import children
from .teams import catalog_specs

if TYPE_CHECKING:
    import pathlib

FAM = "tools_streaming"
ASK = "Return the final result by calling final_output."
BAD: Obj = {"fixed": "yes"}
GOOD: Obj = {"fixed": True}
KID = "0192a000-0000-7000-8000-0000000000d1"
# What a Pydantic model with a nested model writes: titles, a default, and $defs by $ref.
LINE: Obj = {
    "properties": {
        "sku": {"title": "Sku", "type": "string"},
        "qty": {"title": "Qty", "type": "integer"},
    },
    "required": ["sku", "qty"],
    "title": "Line",
    "type": "object",
}
INVOICE: Obj = {
    "$defs": {"Line": LINE},
    "properties": {
        "lines": {"items": {"$ref": "#/$defs/Line"}, "title": "Lines", "type": "array"},
        "note": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None, "title": "Note"},
    },
    "required": ["lines"],
    "title": "Invoice",
    "type": "object",
}


def build(root: pathlib.Path) -> None:
    _exhausted(root)
    _asks(root)
    _nested(root)
    _subagent(root)


def _step(cid: str, inp: Obj) -> Obj:
    use: Obj = {"type": "tool_use", "call_id": cid, "name": "final_output", "input": inp}
    return {"content": [use], "stop_reason": "tool_use", "usage": tokens(60, 8)}


def _asked(question: str) -> Log:
    log = Log()
    started(log, [FINAL_OUTPUT], policy=policy(output=OUTPUT))
    user(log, question)
    return log


def _candidate(cid: str, inp: Obj, outcome: str) -> list[JsonValue]:
    """One request answered by a final_output candidate, and how the runner records it."""
    validated: Obj = {"schema_sha256": OUTPUT["schema_sha256"], "outcome": outcome}
    shown: Obj = {"call_id": cid, "is_error": outcome == "rejected"}
    if outcome == "accepted":
        validated["value"] = inp
        shown["preview"] = canonical(inp).decode()
    return [
        {"type": "model_request", "data": {"attempt": 1}},
        {"type": "model_response", "data": {"content": _step(cid, inp)["content"]}},
        {"type": "tool_call", "data": {"call_id": cid, "name": "final_output", "input": inp}},
        {"type": "permission_decision", "data": {"call_id": cid, "decision": "allow"}},
        {"type": "output_validated", "data": validated},
        {"type": "tool_result", "data": shown},
    ]


def _recover_case(
    root: pathlib.Path,
    meta: tuple[str, str],
    log: Log,
    appended: list[JsonValue],
    script: list[JsonValue],
) -> None:
    write_case(
        root,
        case(meta[0], FAM, "recover", meta[1], model_script="model.json"),
        log,
        {"outcome": "ok", "state": reduce(log, NOW), "appended": appended},
        extra={"model.json": {"responses": script}},
    )


def _exhausted(root: pathlib.Path) -> None:
    _recover_case(
        root,
        (
            "structured-output-retries-exhausted",
            "With max_retries 2, two final_output candidates fail the pinned schema. Each is "
            "recorded as output_validated{rejected} with an error result; the second is the "
            "max_retries-th failed candidate, so the turn ends output_invalid with no further "
            "model call.",
        ),
        _asked("Is the bug fixed? Answer with final_output."),
        [
            *_candidate("call_1", BAD, "rejected"),
            *_candidate("call_2", BAD, "rejected"),
            {"type": "turn_completed", "data": {"reason": "output_invalid"}},
        ],
        [_step("call_1", BAD), _step("call_2", BAD)],
    )


def _asks(root: pathlib.Path) -> None:
    said: Obj = {
        "content": [{"type": "text", "text": "Yes, it is fixed."}],
        "stop_reason": "end_turn",
        "usage": tokens(60, 6),
    }
    ask: Obj = {
        "source": "recovery",
        "trust": "trusted_instruction",
        "origin": {"id": "final_output"},
        "text": ASK,
    }
    _recover_case(
        root,
        (
            "structured-output-plain-text-asks",
            "A turn that ends in plain text while an output schema is pinned is a failed "
            "candidate: the runner injects a trusted ask for final_output and requests again. "
            "The next candidate is accepted, its result previews the value as canonical JSON, "
            "and final_output ends the turn.",
        ),
        _asked("Is the bug fixed?"),
        [
            {"type": "model_request", "data": {"attempt": 1}},
            {"type": "model_response", "data": {"content": said["content"]}},
            {"type": "injected", "data": ask},
            *_candidate("call_1", GOOD, "accepted"),
            {"type": "turn_completed", "data": {"reason": "end_turn"}},
        ],
        [said, _step("call_1", GOOD)],
    )


def _invoice(value: Obj) -> Log:
    """A thread whose pinned output schema nests a model, and an accepted candidate `value`."""
    output: Obj = {**OUTPUT, "schema": INVOICE, "schema_sha256": sha(canonical(INVOICE))}
    tool: Obj = {**FINAL_OUTPUT, "input_schema": INVOICE}
    log = Log()
    started(log, [tool], policy=policy(output=output))
    user(log, "Invoice the order.")
    c = call(log, "final_output", value)
    validated: Obj = {
        "source_event_id": c["event_id"],
        "schema_sha256": output["schema_sha256"],
        "outcome": "accepted",
        "value": value,
    }
    log.add("output_validated", validated)
    return log


def _nested(root: pathlib.Path) -> None:
    good: Obj = {"lines": [{"sku": "A-1", "qty": 2}], "note": None}
    log = _invoice(good)
    result(log, "call_1", canonical(good).decode())
    log.add("turn_completed", {"reason": "end_turn"})
    reduce_case(
        root,
        (
            "output-validated-nested-schema-accepted",
            FAM,
            "The pinned output schema is what a Pydantic model with a nested model writes: "
            "titles and a default (annotations, never constraints) and a $defs entry used by "
            "$ref. An accepted value that satisfies it is valid.",
        ),
        log,
        {},
    )
    reject(
        root,
        (
            "output-validated-nested-schema-mismatch-rejected",
            FAM,
            "output_validated{accepted} whose nested value fails the $ref'd definition (qty "
            '"2" is not an integer): invalid_transition. A reference is checked, never skipped.',
        ),
        _invoice({"lines": [{"sku": "A-1", "qty": "2"}]}),
    )


def _subagent(root: pathlib.Path) -> None:
    log = Log()
    started(log, catalog_specs(("spawn_agent",)))
    user(log, "Ask the checker whether the bug is fixed.")
    call(log, "spawn_agent", {"agent": "checker", "prompt": "Is the bug fixed?"})
    spawned: Obj = {
        "call_id": "call_1",
        "child_thread_id": KID,
        "agent_name": "checker",
        "mode": "foreground",
        "isolation": "none",
    }
    log.add("agent_spawned", spawned)
    shown = canonical(GOOD)
    finished: Obj = {
        "child_thread_id": KID,
        "status": "completed",
        "output_ref": log.art(shown, "text/plain"),
        "usage": tokens(60, 8),
    }
    log.add("agent_finished", finished)
    result(log, "call_1", shown.decode())
    answer(log, "The checker says it is fixed.")
    reduce_case(
        root,
        (
            "subagent-structured-output-finished",
            "agents_teams",
            "A subagent with an output schema reports the canonical JSON of its accepted "
            "output_validated.value: agent_finished.output_ref names exactly those bytes, and "
            "the spawn call's result shows the same text.",
        ),
        log,
        {"children": children(log)},
    )
