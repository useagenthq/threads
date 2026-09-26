"""One direct test per hook (lane 14 E), each driving a public agent().run() in the situation that
fires it. The registry is keyed by the hook names spec/api.json lists, so the guard below fails by
construction when a hook is added and left untested, and every case's normalized hook_decisions are
compared with TypeScript's through spec/conformance/vectors/hook-decisions.json."""

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

import pytest
from agent_kit import AGENT_CASES
from each_kit import Decision, HookCase
from pydantic import BaseModel, ConfigDict, Field
from tools_kit import TOOL_CASES
from turn_kit import TURN_CASES

from threads.hooks.types import CLASSES, wire_name

CASES: Final[Mapping[str, HookCase]] = {**TURN_CASES, **TOOL_CASES, **AGENT_CASES}
"""Every hook's case, keyed as spec/api.json `types.Hooks` names it."""

WIRE: Final[Mapping[str, str]] = {hook: wire_name(hook) for hook in CLASSES}
"""The wire `hook_decision.hook`; only on_stop_failure is named differently there."""

SPEC: Final = Path(__file__).resolve().parents[3] / "spec"


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _HooksType(_Lenient):
    fields: Mapping[str, object]


class _Types(_Lenient):
    hooks: _HooksType = Field(alias="Hooks")


class _Api(_Lenient):
    types: _Types


class _Divergence(_Lenient):
    hook: str
    field: str
    note: str


class _Vector(_Lenient):
    doc: str
    known_divergences: tuple[_Divergence, ...]
    decisions: Mapping[str, tuple[Mapping[str, str | int], ...]]


HOOKS: Final[tuple[str, ...]] = tuple(
    _Api.model_validate_json((SPEC / "api.json").read_bytes()).types.hooks.fields
)
VECTOR: Final = _Vector.model_validate_json(
    (SPEC / "conformance" / "vectors" / "hook-decisions.json").read_bytes()
)


def _shed(hook: str, rows: Sequence[Decision]) -> list[dict[str, str | int]]:
    """Without the fields this hook's decision is not yet compared on."""
    drop = {d.field for d in VECTOR.known_divergences if d.hook == WIRE[hook]}
    return [{k: v for k, v in row.items() if k not in drop} for row in rows]


def test_every_hook_the_api_lists_has_a_case() -> None:
    assert sorted(CASES) == sorted(HOOKS)


def test_the_vector_holds_the_decisions_of_every_hooks_case() -> None:
    assert sorted(VECTOR.decisions) == sorted(HOOKS)


@pytest.mark.parametrize("hook", HOOKS)
def test_each_hook_on_a_public_agent_run(hook: str) -> None:
    produced = asyncio.run(CASES[hook]())
    # Whatever the case decided is recorded under the wire hook name.
    assert {str(d["hook"]) for d in produced} == ({WIRE[hook]} if produced else set())
    assert _shed(hook, produced) == _shed(hook, VECTOR.decisions[hook])
