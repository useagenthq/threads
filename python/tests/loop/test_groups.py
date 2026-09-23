"""The shared grouping vector (spec/conformance/vectors/tool-groups.json): both runtimes plan the
same groups and barriers for the same pending calls."""

from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

from threads.log import EffectClass
from threads.loop.groups import Candidate, groups

SPEC = Path(__file__).resolve().parents[3] / "spec"


class _Call(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    name: str
    effect_class: EffectClass
    concurrent: bool
    framework: bool
    ends_turn: bool
    decision: Literal["allow", "ask", "deny", "none"]


class _Case(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    name: str
    pending: tuple[_Call, ...]
    plan: tuple[tuple[int, ...], ...]


class _Vector(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    description: str
    cases: tuple[_Case, ...]
    recorded_order: tuple[str, ...]


VECTOR = _Vector.model_validate_json(
    (SPEC / "conformance" / "vectors" / "tool-groups.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", VECTOR.cases, ids=lambda c: c.name)
def test_groups_match_the_vector(case: _Case) -> None:
    pending = [
        Candidate(c.concurrent, c.effect_class, c.framework, c.ends_turn, c.decision)
        for c in case.pending
    ]
    assert groups(pending) == case.plan


def test_no_pending_calls_no_plan() -> None:
    assert groups([]) == ()
