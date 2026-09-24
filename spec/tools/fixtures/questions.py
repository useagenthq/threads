# pyright: strict
"""ask_user questions (spec/schema/README.md, "Questions and remembered rules"): the answer
vector both runtimes replay, and the cases for parking, expiry and strict options. The vector's
decisions are authored from the rules, never read from an implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, CASES, DAY, NOW, T0
from .log import Log, reduce
from .memory import catalog_spec
from .pieces import (
    FINAL,
    NO_MODEL,
    TAIL,
    call,
    case,
    dump,
    reduce_case,
    reject,
    started,
    user,
    write_case,
)

if TYPE_CHECKING:
    import pathlib

    from .jcs import JsonValue, Obj

FAM = "permissions_approvals"
ASK = catalog_spec("ask_user", "read_only")
VECTOR = CASES.parent / "vectors" / "ask-user-answers.json"
ADDRESS: Obj = {"kind": "input", "id": "call_1"}

RGB: Obj = {"question": "Which color?", "options": ["red", "blue"]}
RGB_MULTI: Obj = {**RGB, "multi_select": True}
STRASSE: Obj = {"question": "Street?", "options": ["Stra\u00dfe", "STRASSE"]}
NUMERIC: Obj = {"question": "How many?", "options": ["1", "10"]}
SIGMA: Obj = {"question": "Road?", "options": ["\u039f\u0394\u039f\u03a3", "x"]}
FREE: Obj = {"question": "Staging or production?"}

# (input, valid): inputs the schema accepts, judged by the question rules (rule 46).
INPUTS: tuple[tuple[Obj, bool], ...] = (
    (FREE, True),
    (RGB, True),
    (RGB_MULTI, True),
    (STRASSE, True),
    (NUMERIC, True),
    (SIGMA, True),
    ({"question": "Ok?", "options": ["Yes", " yes "]}, False),
    ({"question": "Ok?", "options": ["\u00a0Yes", "yes"]}, False),
    ({"question": "Ok?", "options": ["Yes\u0085", "yes"]}, True),
    ({"question": "Ok?", "options": ["x", "\u00a0 "]}, False),
    ({"question": "Ok?", "multi_select": True}, False),
    ({"question": "Ok?", "multi_select": False}, True),
    ({"question": "Ok?", "options": ["a, b", "c"]}, True),
    ({"question": "Ok?", "options": ["a, b", "c"], "multi_select": True}, False),
    ({"question": "Ok?", "options": ["a\nb", "c"], "multi_select": True}, False),
    ({"question": "Ok?", "options": ["a\u2028b", "c"], "multi_select": True}, False),
)

# (input, reply, recorded answer or None when the reply is not an answer).
REPLIES: tuple[tuple[Obj, JsonValue, str | None], ...] = (
    (RGB, "2", "blue"),
    (RGB, " RED ", "red"),
    (RGB, "\u2003blue\u3000", "blue"),
    (RGB, "02", "blue"),
    (RGB, "3", None),
    (RGB, "0", None),
    (RGB, "green", None),
    (RGB, "", None),
    (RGB, "red, blue", None),
    (RGB, "2\u0085", None),
    (RGB, "\u0662", None),
    (RGB, ["red"], "red"),
    (RGB, ["red", "blue"], None),
    (STRASSE, "STRASSE", "STRASSE"),
    (STRASSE, "strasse", "STRASSE"),
    (STRASSE, "STRA\u00dfE", "Stra\u00dfe"),
    (NUMERIC, "1", "1"),
    (NUMERIC, "10", "10"),
    (NUMERIC, "2", None),
    (SIGMA, "\u03bf\u03b4\u03bf\u03c2", None),
    (SIGMA, "\u039f\u0394\u039f\u03a3", "\u039f\u0394\u039f\u03a3"),
    (SIGMA, "2", "x"),
    (RGB_MULTI, "red, RED\n", "red"),
    (RGB_MULTI, "BLUE, red, Blue", "blue\nred"),
    (RGB_MULTI, "2,1", "blue\nred"),
    (RGB_MULTI, "red\u2028blue", "red\nblue"),
    (RGB_MULTI, ", ,", None),
    (RGB_MULTI, "red, green", None),
    (RGB_MULTI, ["red", "blue"], "red\nblue"),
    (RGB_MULTI, ["blue", "2"], "blue"),
    (FREE, "Staging.", "Staging."),
    (FREE, " Staging. ", " Staging. "),
    (FREE, "   ", None),
    (FREE, ["staging, eu", "production"], "staging, eu\nproduction"),
)

# (input, question message, correction message)
MESSAGES: tuple[tuple[Obj, str, str], ...] = (
    (FREE, "Staging or production?", "Please answer with some text."),
    (
        RGB,
        "Which color?\n\n1. red\n2. blue\n\nReply with the number or the text of your choice.",
        "Please answer with one of:\n\n1. red\n2. blue\n\n"
        "Reply with the number or the text of your choice.",
    ),
    (
        RGB_MULTI,
        "Which color?\n\n1. red\n2. blue\n\n"
        "Reply with the number or the text of each choice, separated by commas.",
        "Please answer with one of:\n\n1. red\n2. blue\n\n"
        "Reply with the number or the text of each choice, separated by commas.",
    ),
    (
        NUMERIC,
        "How many?\n\n- 1\n- 10\n\nReply with the exact text of your choice.",
        "Please answer with one of:\n\n- 1\n- 10\n\nReply with the exact text of your choice.",
    ),
)


def _vector() -> str:
    doc: Obj = {
        "description": (
            "ask_user's question rules and strict answers (spec/schema/README.md, "
            '"Questions and remembered rules"). inputs: schema-valid ask_user inputs and whether '
            "rule 46 accepts them. replies: an accepted input, a reply (text, or a list as "
            "Thread.answer takes it) and the recorded answer, or null when it is invalid_answer. "
            "messages: the question and correction texts a channel shows."
        ),
        "inputs": [{"input": i, "valid": v} for i, v in INPUTS],
        "replies": [{"input": i, "reply": r, "answer": a} for i, r, a in REPLIES],
        "messages": [{"input": i, "question": q, "correction": c} for i, q, c in MESSAGES],
    }
    return dump(doc)


def write() -> None:
    VECTOR.write_text(_vector(), encoding="utf-8")


def check() -> list[str]:
    current = VECTOR.read_text(encoding="utf-8") if VECTOR.exists() else ""
    return [] if _vector() == current else [f"{VECTOR.name}: differs; run gen_fixtures.py"]


def build(root: pathlib.Path) -> None:
    _park(root)
    _expire(root)
    _invalid_input(root)
    _strict(root)
    _rejected(root)


def _asked(inp: Obj) -> Log:
    log = Log()
    started(log, [ASK])
    user(log, "Deploy it.")
    call(log, "ask_user", inp)
    return log


def _parked(inp: Obj, expires_at: int = T0 + DAY) -> Log:
    log = _asked(inp)
    log.add("parked", {"address": ADDRESS, "reason": "awaiting_input", "expires_at": expires_at})
    return log


def _park(root: pathlib.Path) -> None:
    log = _asked(RGB)
    write_case(
        root,
        case(
            "ask-user-parks-on-call",
            FAM,
            "recover",
            "An allowed ask_user call with valid options parks the branch on {input, call_id} "
            "for 24 hours; the model is not called.",
            model_script="model.json",
        ),
        log,
        {
            "outcome": "ok",
            "state": reduce(log, NOW),
            "appended": [
                {
                    "type": "parked",
                    "actor_kind": "host",
                    "epoch": 2,
                    "data": {
                        "address": ADDRESS,
                        "reason": "awaiting_input",
                        "expires_at": NOW + DAY,
                    },
                }
            ],
        },
        extra={"model.json": NO_MODEL},
    )


def _expire(root: pathlib.Path) -> None:
    log = _parked(RGB)
    late = T0 + DAY + 5_000
    write_case(
        root,
        case(
            "ask-user-expired-no-answer",
            FAM,
            "recover",
            "A question past its expires_at with no answer: the next run records the error "
            "result no answer (not_executed) and resumed, then the turn goes on.",
            late,
            model_script="model.json",
        ),
        log,
        {
            "outcome": "ok",
            "state": reduce(log, late),
            "appended": [
                {
                    "type": "tool_result",
                    "actor_kind": "host",
                    "epoch": 2,
                    "data": {
                        "call_id": "call_1",
                        "is_error": True,
                        "origin": "not_executed",
                        "completeness": "complete",
                        "preview": "no answer",
                    },
                },
                {"type": "resumed", "actor_kind": "host", "data": {"address": ADDRESS}},
                *TAIL,
            ],
        },
        extra={"model.json": {"responses": [FINAL]}},
    )


def _invalid_input(root: pathlib.Path) -> None:
    log = _asked({"question": "Ok?", "options": ["Yes", " yes "]})
    write_case(
        root,
        case(
            "ask-user-invalid-options-closed",
            FAM,
            "recover",
            "ask_user options that are equal under the matching rule: the call is closed with "
            "an error result (not_executed) and never parks; the model sees the error.",
            model_script="model.json",
        ),
        log,
        {
            "outcome": "ok",
            "state": reduce(log, NOW),
            "appended": [
                {
                    "type": "tool_result",
                    "epoch": 2,
                    "data": {"call_id": "call_1", "is_error": True, "origin": "not_executed"},
                },
                *TAIL,
            ],
        },
        extra={"model.json": {"responses": [FINAL]}},
    )
    reject(
        root,
        (
            "ask-user-invalid-options-park-rejected",
            FAM,
            "A park on {input, call_id} whose ask_user options are equal under the matching "
            "rule (rule 46): invalid_transition.",
        ),
        _parked({"question": "Ok?", "options": ["Yes", " yes "]}),
    )


def _answered(inp: Obj, preview: str) -> Log:
    log = _parked(inp)
    ans = log.add(
        "tool_result",
        {
            "call_id": "call_1",
            "is_error": False,
            "completeness": "complete",
            "preview": preview,
            "origin": "answered",
        },
        actor="user",
        principal=ALICE,
    )
    log.add("resumed", {"address": ADDRESS, "cause_event_id": ans["event_id"]})
    return log


def _strict(root: pathlib.Path) -> None:
    reject(
        root,
        (
            "ask-user-answer-not-an-option-rejected",
            FAM,
            "An answered tool_result whose preview is none of the ask_user options "
            "(rule 25): invalid_transition.",
        ),
        _answered(RGB, "green"),
    )
    log = _answered(RGB_MULTI, "blue\nred")
    reduce_case(
        root,
        (
            "ask-user-multi-select-joined",
            FAM,
            "A multi_select answer records the chosen options' offered spellings joined "
            "with a newline, in reply order; the turn is open again.",
        ),
        log,
        {},
    )


def _rejected(root: pathlib.Path) -> None:
    log = _parked(RGB)
    reply = log.add(
        "channel_delivery",
        {
            "channel": "slack",
            "installation": "T1",
            "conversation": "C1",
            "delivery_id": "d1",
            "item_key": "d1#0",
            "text": "green",
        },
        actor="channel",
        principal=ALICE,
    )
    log.add(
        "answer_rejected",
        {"call_id": "call_1", "delivery_event_id": reply["event_id"], "reason": "not_an_option"},
    )
    reduce_case(
        root,
        (
            "ask-user-rejected-answer-keeps-question-open",
            FAM,
            "The asker's channel reply matched no option: channel_delivery then "
            "answer_rejected, and the branch stays parked on the question (rule 47).",
        ),
        log,
        {},
    )
    log = _asked(RGB)
    log.add(
        "answer_rejected",
        {
            "call_id": "call_1",
            "delivery_event_id": log.events[-1]["event_id"],
            "reason": "not_an_option",
        },
    )
    reject(
        root,
        (
            "ask-user-rejected-without-question-rejected",
            FAM,
            "answer_rejected for a call with no open question (rule 47): invalid_transition.",
        ),
        log,
    )
