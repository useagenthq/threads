# pyright: strict
"""Checks spec/conformance/coverage.json against the corpus and, when given, a scenario list."""

from __future__ import annotations

import json
import os
import pathlib
import re
from typing import TYPE_CHECKING

from .common import CASES, arr, obj, text

if TYPE_CHECKING:
    from .jcs import JsonValue

COVERAGE = CASES.parent / "coverage.json"
ROOT = CASES.parents[2]
# An optional maintainer scenario list (THREADS_SCENARIO_LIST); CI checks the corpus side alone.
_LIST = os.environ.get("THREADS_SCENARIO_LIST")
BRIEF = pathlib.Path(_LIST) if _LIST else None
SCENARIO = re.compile(r"\s*- \*\*[✓✗] (F\d+\.\d+)(?: \[step 1[^\]]*\])?\*\* → (.*)")
EVIDENCE = re.compile(r"(integration test \([^)]*\) )?`([a-z0-9-]+)`")
KINDS = frozenset({"case", "test", "job", "live_gate"})
STATUSES = frozenset({"planned", "implemented"})


def _brief(path: pathlib.Path) -> dict[str, list[tuple[bool, str]]]:
    """Scenario id -> [(is_live_gate, ref)] in list order."""
    found: dict[str, list[tuple[bool, str]]] = {}
    for line in path.read_text().splitlines():
        m = SCENARIO.match(line)
        if m:
            found[m[1]] = [(r[1] is not None, r[2]) for r in EVIDENCE.finditer(m[2])]
    return found


def _job_tests(where: str, tests: JsonValue) -> list[str]:
    """An implemented job names the test files that run it (repo-relative), and they exist."""
    paths = [text(t) for t in arr(tests)]
    if not paths:
        return [f"{where}: an implemented job lists its tests"]
    return [f"{where}: no test file {p}" for p in paths if not (ROOT / p).is_file()]


def check(cases: pathlib.Path) -> list[str]:
    """Returns problems: bad kinds or statuses, case status out of step with the corpus, and
    (when a scenario list is given) scenarios or evidence that differ from the brief."""
    problems: list[str] = []
    mapped: dict[str, list[tuple[bool, str]]] = {}
    for s in arr(obj(json.loads(COVERAGE.read_text()))["scenarios"]):
        sid = text(obj(s)["id"])
        mapped[sid] = []
        for x in arr(obj(s)["evidence"]):
            ev = obj(x)
            kind, ref, status = text(ev["kind"]), text(ev["ref"]), text(ev["status"])
            mapped[sid].append((kind == "live_gate", ref))
            if kind not in KINDS or status not in STATUSES:
                problems.append(f"{sid} {ref}: bad kind or status")
            elif kind == "case" and (status == "implemented") != (cases / ref).is_dir():
                problems.append(f"{sid} {ref}: status {status} disagrees with the corpus")
            elif kind == "job" and status == "implemented":
                problems += _job_tests(f"{sid} {ref}", ev.get("tests", []))
    if BRIEF is not None and BRIEF.is_file():
        brief = _brief(BRIEF)
        problems += [
            f"{sid}: in the scenario list, not in coverage.json" for sid in brief.keys() - mapped
        ]
        problems += [
            f"{sid}: in coverage.json, not in the scenario list" for sid in mapped.keys() - brief
        ]
        problems += [
            f"{sid}: evidence differs from the scenario list"
            for sid in brief.keys() & mapped.keys()
            if brief[sid] != mapped[sid]
        ]
    return problems
