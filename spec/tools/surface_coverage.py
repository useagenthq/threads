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
from dataclasses import dataclass
from typing import TYPE_CHECKING

from surface_contract import LANGS, Gap, Member, needs_coverage, obj

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Mapping

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
    """Names that need a test in this language: not missing themselves, and for an option, its
    function or method isn't missing. A member of a type missing from its entry still exists,
    unless the type is missing in both languages: a type built nowhere yet has nothing to test."""
    missing = {g.name for g in gaps if g.lang == lang and g.kind == "missing"}
    keys = {(g.name, g.lang) for g in gaps if g.kind == "missing"}
    nowhere = {name for name, _ in keys if all((name, lg) in keys for lg in LANGS)}
    return {
        m.name
        for m in contract.values()
        if needs_coverage(m)
        and lang in m.langs
        and m.name not in missing
        and not (m.role == "option" and m.parent in missing)
        and m.parent not in nowhere
    }


def _entry_ids(name: str, entry: Json, lang: str) -> tuple[list[str], list[str]]:
    langs = obj(entry)
    if not isinstance(entry, dict) or not langs or not set(langs) <= {"ts", "py"}:
        return [], [f"api-coverage.json {name}: expected {{ts?, py?}} lists of test IDs"]
    ids = langs.get(lang)
    if ids is None:
        return [], []
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids):
        return [], [f"api-coverage.json {name}.{lang}: expected a non-empty list of test IDs"]
    return [i for i in ids if isinstance(i, str)], []


@dataclass(frozen=True, slots=True)
class Owed:
    """What needs test evidence: the contract's members (minus listed gaps), and the factory
    evidence names (`exa!missing_secret`, `e2b.lifetime_ms=default`) with the languages each is
    owed in (factory_coverage.py)."""

    contract: dict[str, Member]
    gaps: list[Gap]
    factories: Mapping[str, frozenset[str]]

    def expected(self, lang: str) -> set[str]:
        owed = {n for n, langs in self.factories.items() if lang in langs}
        return _expected(self.contract, self.gaps, lang) | owed

    def unknown(self, name: str, listed: bool, lang: str) -> str | None:
        """Why an entry shouldn't be in the registry, or None."""
        if name in self.factories:
            wrong = listed and lang not in self.factories[name]
            return (
                f"api-coverage.json {name}.{lang}: not owed in {lang}; delete it" if wrong else None
            )
        member = self.contract.get(name)
        if member is None or not needs_coverage(member):
            return (
                f"api-coverage.json {name}: not a function, required method or required option "
                "in spec/api.json; delete the entry"
            )
        if listed and name not in self.expected(lang):
            return (
                f"api-coverage.json {name}.{lang}: not a function, required method or required "
                f"option that exists in {lang}; delete the entry"
            )
        return None


def check_coverage(doc: Json, owed: Owed, lang: str, junit: pathlib.Path) -> list[str]:
    if not isinstance(doc, dict):
        return ["api-coverage.json: expected an object of {name: {ts?, py?}}"]
    errs: list[str] = []
    results, more = junit_results(junit, lang)
    errs += more
    for name, entry in doc.items():
        ids, problems = _entry_ids(name, entry, lang)
        errs += problems
        why = owed.unknown(name, bool(ids), lang)
        errs += [why] if why else []
        for test_id in [] if more else ids:
            outcome = results.get(normalize(test_id, lang), "absent from the JUnit report")
            if outcome != "passed":
                errs.append(f"api-coverage.json {name}.{lang}: test {test_id!r} {outcome}")
    listed = {n for n, e in doc.items() if _entry_ids(n, e, lang)[0]}
    errs += [
        f"api-coverage.json: {name} has no {lang} test; add the ID of a test that exercises it"
        for name in sorted(owed.expected(lang) - listed)
    ]
    return errs
