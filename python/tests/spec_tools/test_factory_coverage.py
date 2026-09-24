"""Factory test evidence in the coverage registry (spec/tools/factory_coverage.py through
surface_coverage.check_coverage): `<factory>!<code>` per applicable language and
`<factory>.<option>=default` unless review-only."""

import pathlib
from typing import TYPE_CHECKING

from factory_coverage import factory_evidence
from factory_kit import Obj, api
from surface_contract import Gap, Member
from surface_coverage import Owed, check_coverage
from surface_kit import PY_JUNIT, TS_JUNIT

if TYPE_CHECKING:
    from api_factories import Json

PASSED: Obj = {"ts": ["packages/a.test.ts > agent > runs"], "py": ["tests/test_a.py::test_runs"]}
LIFETIME: Obj = {"name": "lifetime_ms", "kind": "option", "default": 3600000, "required": False}
TRANSPORT: Obj = {"name": "transport", "kind": "positional", "lang": "ts", "default_doc": "net"}


def evidence(**extra: Obj) -> dict[str, frozenset[str]]:
    errors: Json = [{"code": "missing_secret"}, {"code": "invalid_config", "lang": "py"}]
    contract = api(config_errors=errors, params=[LIFETIME, TRANSPORT])
    decisions: Obj = {"factories": {"fetcher": {"owner": "15B", **extra}}}
    return factory_evidence(contract, decisions)


def test_codes_expand_per_language_and_defaults_owe_behavior_tests() -> None:
    assert evidence() == {
        "fetcher!missing_secret": frozenset({"ts", "py"}),
        "fetcher!invalid_config": frozenset({"py"}),
        "fetcher.lifetime_ms=default": frozenset({"ts", "py"}),
        "fetcher.transport=default": frozenset({"ts"}),
    }
    review: Obj = {"transport": "Seam."}
    assert "fetcher.transport=default" not in evidence(review_only=review)


def gate(tmp_path: pathlib.Path, doc: Obj, lang: str) -> list[str]:
    junit = tmp_path / f"{lang}.xml"
    junit.write_text(TS_JUNIT if lang == "ts" else PY_JUNIT)
    owed = {"fetcher!invalid_config": frozenset({"py"})}
    return check_coverage(doc, Owed({}, [], owed), lang, junit)


def test_an_owed_refusal_test_is_required_in_its_language_only(tmp_path: pathlib.Path) -> None:
    assert gate(tmp_path, {}, "py") == [
        "api-coverage.json: fetcher!invalid_config has no py test; add the ID of a test that "
        "exercises it"
    ]
    assert gate(tmp_path, {}, "ts") == []
    assert gate(tmp_path, {"fetcher!invalid_config": {"py": PASSED["py"]}}, "py") == []


def test_a_test_for_a_language_the_code_does_not_apply_to_is_red(tmp_path: pathlib.Path) -> None:
    assert gate(tmp_path, {"fetcher!invalid_config": PASSED}, "ts") == [
        "api-coverage.json fetcher!invalid_config.ts: not owed in ts; delete it"
    ]


def test_an_owed_test_must_have_passed(tmp_path: pathlib.Path) -> None:
    failing: Obj = {"fetcher!invalid_config": {"py": ["tests/test_a.py::test_missing"]}}
    assert gate(tmp_path, failing, "py") == [
        "api-coverage.json fetcher!invalid_config.py: test 'tests/test_a.py::test_missing' "
        "absent from the JUnit report"
    ]


def test_a_type_built_nowhere_owes_no_method_tests() -> None:
    """A method of a type missing in both languages has nothing to test; one missing in only
    one language still owes its tests there (its members exist)."""
    langs = frozenset({"ts", "py"})
    method = Member("Team.start", "method", langs, True, "core", "start", "start", "Team")
    contract = {"Team.start": method}
    both = [Gap("Team", lang, "missing", "90-ma-p1") for lang in ("ts", "py")]
    assert Owed(contract, both, {}).expected("py") == set()
    assert Owed(contract, both[1:], {}).expected("py") == {"Team.start"}
