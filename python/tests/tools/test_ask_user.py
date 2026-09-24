"""The shared ask_user vector (spec/conformance/vectors/ask-user-answers.json): the question
rules, strict answers and channel messages, byte for byte as TypeScript."""

import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from threads._generated.tools_v1 import AskUserInput
from threads.log.ask_user import (
    ask_of,
    ask_problem,
    correction_text,
    match_answer,
    question_text,
)

VECTOR: dict[str, JsonValue] = json.loads(
    (
        Path(__file__).resolve().parents[3] / "spec/conformance/vectors/ask-user-answers.json"
    ).read_text(encoding="utf-8")
)


def _cases(key: str) -> list[dict[str, JsonValue]]:
    cases = VECTOR[key]
    assert isinstance(cases, list)
    return [c for c in cases if isinstance(c, dict)]


@pytest.mark.parametrize("case", _cases("inputs"))
def test_the_question_rules_accept_exactly_the_valid_inputs(case: dict[str, JsonValue]) -> None:
    ask = AskUserInput.model_validate(case["input"])
    assert (ask_problem(ask) is None) is case["valid"]
    assert (ask_of(case["input"]) is not None) is case["valid"]


@pytest.mark.parametrize("case", _cases("replies"))
def test_a_reply_records_its_answer_or_is_invalid(case: dict[str, JsonValue]) -> None:
    ask = AskUserInput.model_validate(case["input"])
    reply = case["reply"]
    assert isinstance(reply, (str, list))
    items = reply if isinstance(reply, str) else [str(r) for r in reply]
    assert match_answer(ask, items) == case["answer"]


@pytest.mark.parametrize("case", _cases("messages"))
def test_question_and_correction_messages_are_the_shared_bytes(
    case: dict[str, JsonValue],
) -> None:
    ask = AskUserInput.model_validate(case["input"])
    assert question_text(ask) == case["question"]
    assert correction_text(ask) == case["correction"]
