"""The `fork` conformance cases against the fake sandbox (fork_kit.py runs them)."""

import asyncio

import pytest
from corpus import CASES, cases, load, obj
from fork_kit import Outcome, assert_expected, run_case, script_of
from pydantic import JsonValue

from threads.sandbox import fake_sandbox


def run_fake(name: str, script: dict[str, JsonValue] | None = None) -> Outcome:
    case = CASES / name
    sandbox = fake_sandbox(script_of(case) if script is None else script)
    return asyncio.run(run_case(case, sandbox, lambda: sandbox.creates))


@pytest.mark.parametrize("name", cases("fork"))
def test_fork_case(name: str) -> None:
    assert_expected(name, run_fake(name))


def test_an_interrupted_fork_never_calls_an_unproven_resource_released() -> None:
    """recovery parks a restore it can't find by key as unknown; only a final
    not_found or a confirmed release counts as released."""
    case = CASES / "fork-crash-no-orphan"
    snap = obj(obj(load(case, "sandbox.json")["snapshots"])["snap_01"])
    unprovable: dict[str, JsonValue] = {
        "snapshots": {"snap_01": {**snap, "create_lookup": "unsupported"}}
    }
    got = run_fake("fork-crash-no-orphan", unprovable)
    assert (got.child_state, got.rows) == ("fork_failed", (("sandbox", "unknown"),))
