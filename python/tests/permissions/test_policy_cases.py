"""Conformance runner for the `policy` kind (spec/conformance/README.md).

Builds the engine from `input.permissions`, decides each call under its own mode and, with
`input.ceiling`, under the ceiling too. Unknown keys in any fixture file fail the case.
"""

import json
from pathlib import Path
from typing import ClassVar, Literal

import pytest
from pydantic import BaseModel, ConfigDict, JsonValue

from threads.log import PermissionMode, Permissions
from threads.permissions import Call, Category, Decision, Source, Verdict, decide, decide_capped

CASES = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "cases"
MIN_POLICY_CASES = 4


class _Model(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", strict=True, frozen=True)


class PolicyCall(_Model):
    mode: PermissionMode
    tool: str
    category: Category
    input: dict[str, JsonValue]


class PolicyInput(_Model):
    workspace: str
    permissions: Permissions
    calls: list[PolicyCall]
    ceiling: Permissions | None = None


class PolicyCase(_Model):
    name: str
    family: str
    kind: Literal["policy"]
    description: str
    clock: dict[str, int]
    input: PolicyInput


class ExpectedDecision(_Model):
    decision: Verdict
    source: Source
    rule: str | None = None


class PolicyExpected(_Model):
    outcome: Literal["ok"]
    decisions: list[ExpectedDecision]


def _kind(case: Path) -> JsonValue:
    meta: dict[str, JsonValue] = json.loads((case / "case.json").read_text(encoding="utf-8"))
    return meta["kind"]


POLICY_CASES = sorted(d.name for d in CASES.iterdir() if _kind(d) == "policy")


def test_policy_cases_exist() -> None:
    assert len(POLICY_CASES) >= MIN_POLICY_CASES


def _decide(given: PolicyInput, call: PolicyCall) -> Decision:
    request = Call(call.tool, call.category, call.input)
    if given.ceiling is None:
        return decide(given.permissions, given.workspace, call.mode, request)
    return decide_capped(given.permissions, given.ceiling, given.workspace, call.mode, request)


@pytest.mark.parametrize("name", POLICY_CASES)
def test_policy_case(name: str) -> None:
    case = CASES / name
    meta = PolicyCase.model_validate_json((case / "case.json").read_bytes())
    expected = PolicyExpected.model_validate_json((case / "expected.json").read_bytes())
    calls = meta.input.calls
    assert len(calls) == len(expected.decisions)
    for index, (call, want) in enumerate(zip(calls, expected.decisions, strict=True)):
        got = _decide(meta.input, call)
        assert (got.decision, got.source) == (want.decision, want.source), (index, call)
        if want.rule is not None:
            assert got.rule == want.rule, (index, call)
