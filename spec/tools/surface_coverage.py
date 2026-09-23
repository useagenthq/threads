# pyright: strict
"""Test evidence for the API surface (spec/api-coverage.json) against a JUnit report.

The claim is exactly "a named, reviewed test exists and passed in this CI run", not that the test
exercised the member: which test covers a name is a reviewed statement, not something CI can see.
Stdlib only.

IDs: TypeScript `<file> > <describe>... > <test>` (paths relative to typescript/, as bun reports
them); Python `tests/x.py::test_y` or `tests/x.py::Class::test_y` (relative to python/).
"""

from __future__ import annotations

# Our own CI's test report, not untrusted input.
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

from surface_contract import Gap, Member, needs_coverage, obj

if TYPE_CHECKING:
    import pathlib

    from check_api import Json

OUTCOMES = ("failure", "error", "skipped")


def _outcome(case: ET.Element) -> str:
    return next((o for o in OUTCOMES if case.find(o) is not None), "passed")


def _bun(suite: ET.Element, path: tuple[str, ...], out: dict[str, str]) -> None:
    here = (*path, suite.get("name", ""))
    for case in suite.findall("testcase"):
        key = " > ".join((*here, case.get("name", "")))
        out[key] = "failure" if out.get(key, "passed") != "passed" else _outcome(case)
    for child in suite.findall("testsuite"):
        _bun(child, here, out)


def _pytest(root: ET.Element, out: dict[str, str]) -> None:
    for case in root.iter("testcase"):
        key = f"{case.get('classname', '')}::{case.get('name', '')}"
        out[key] = "failure" if out.get(key, "passed") != "passed" else _outcome(case)


def junit_results(path: pathlib.Path, lang: str) -> tuple[dict[str, str], list[str]]:
    """Each test's outcome (passed, failure, error or skipped) by its normalized ID."""
    try:
        root = ET.parse(path).getroot()  # noqa: S314 - our own test report (see the import)
    except (OSError, ET.ParseError) as e:
        return {}, [f"surface gate: can't read the JUnit report {path}: {e}"]
    out: dict[str, str] = {}
    if lang == "py":
        _pytest(root, out)
    else:
        for suite in root.findall("testsuite"):
            _bun(suite, (), out)
    return out, []


def normalize(test_id: str, lang: str) -> str:
    """A pytest ID as JUnit names it: tests/x.py::C::t is classname tests.x.C, name t."""
    if lang == "ts":
        return test_id
    file, _, rest = test_id.partition("::")
    *classes, name = rest.split("::")
    return f"{'.'.join([file.removesuffix('.py').replace('/', '.'), *classes])}::{name}"


def _expected(contract: dict[str, Member], gaps: list[Gap], lang: str) -> set[str]:
    missing = {g.name for g in gaps if g.lang == lang and g.kind == "missing"}
    return {
        m.name
        for m in contract.values()
        if needs_coverage(m) and lang in m.langs and not {m.name, m.parent} & missing
    }


def _entry_ids(name: str, entry: Json, lang: str) -> tuple[list[str], list[str]]:
    langs = obj(entry)
    if not isinstance(entry, dict) or not set(langs) <= {"ts", "py"}:
        return [], [f"api-coverage.json {name}: expected {{ts?, py?}} lists of test IDs"]
    ids = langs.get(lang)
    if ids is None:
        return [], []
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids):
        return [], [f"api-coverage.json {name}.{lang}: expected a non-empty list of test IDs"]
    return [i for i in ids if isinstance(i, str)], []


def check_coverage(
    doc: Json, contract: dict[str, Member], gaps: list[Gap], lang: str, junit: pathlib.Path
) -> list[str]:
    if not isinstance(doc, dict):
        return ["api-coverage.json: expected an object of {name: {ts?, py?}}"]
    expected = _expected(contract, gaps, lang)
    errs: list[str] = []
    results, more = junit_results(junit, lang)
    errs += more
    for name, entry in doc.items():
        ids, problems = _entry_ids(name, entry, lang)
        errs += problems
        if ids and name not in expected:
            errs.append(
                f"api-coverage.json {name}.{lang}: not a function, required method or required "
                f"option that exists in {lang}; delete the entry"
            )
        for test_id in [] if more else ids:
            outcome = results.get(normalize(test_id, lang), "absent from the JUnit report")
            if outcome != "passed":
                errs.append(f"api-coverage.json {name}.{lang}: test {test_id!r} {outcome}")
    listed = {n for n, e in doc.items() if _entry_ids(n, e, lang)[0]}
    errs += [
        f"api-coverage.json: {name} has no {lang} test; add the ID of a test that exercises it"
        for name in sorted(expected - listed)
    ]
    return errs
