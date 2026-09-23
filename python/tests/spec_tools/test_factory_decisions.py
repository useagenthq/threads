"""The factory decisions file against api.json (spec/tools/factory_decisions.py)."""

import json
import pathlib
from typing import TYPE_CHECKING

from factory_decisions import check_decisions, check_factory_contract
from factory_kit import Obj, api, decisions, sources

if TYPE_CHECKING:
    from api_factories import Json

SPEC = pathlib.Path(__file__).resolve().parents[3] / "spec"
TRANSPORT: Obj = {"name": "transport", "kind": "positional", "lang": "ts", "default_doc": "net"}


def test_the_real_contract_and_decisions_pass() -> None:
    api_doc: Json = json.loads((SPEC / "api.json").read_text())
    meta: Json = json.loads((SPEC / "schema" / "api.schema.json").read_text())
    assert check_factory_contract(api_doc, meta, SPEC) == []


def test_a_matching_fixture_passes(tmp_path: pathlib.Path) -> None:
    assert check_decisions(api(), decisions(), sources(tmp_path)) == []


def test_an_installed_factory_missing_from_the_decisions_is_red(tmp_path: pathlib.Path) -> None:
    doc = decisions()
    doc["factories"] = {}
    doc["decisions"] = []
    assert check_decisions(api(), doc, sources(tmp_path)) == [
        "decisions: factory fetcher is in spec/api.json; add it to factories"
    ]


def test_pending_and_installed_must_agree(tmp_path: pathlib.Path) -> None:
    doc = decisions()
    doc["factories"] = {"fetcher": {"owner": "15A", "pending": "Soon."}, "later": {"owner": "15B"}}
    assert check_decisions(api(), doc, sources(tmp_path)) == [
        "decisions factories.fetcher: installed in spec/api.json; drop pending",
        "decisions factories.later: not in spec/api.json; mark it pending",
    ]


def test_a_default_needs_a_review_only_reason(tmp_path: pathlib.Path) -> None:
    doc = decisions()
    doc["decisions"] = [
        {
            "factory": "fetcher",
            "option": "transport",
            "lang": "ts",
            "decision": "S.",
            "owner": "15A",
        }
    ]
    assert check_decisions(api(params=[TRANSPORT]), doc, sources(tmp_path)) == [
        "api.json fetcher.transport: a default needs a behavior test or a review_only reason"
    ]
    doc["factories"] = {"fetcher": {"owner": "15A", "review_only": {"transport": "Seam."}}}
    assert check_decisions(api(params=[TRANSPORT]), doc, sources(tmp_path)) == []


def test_a_one_language_param_needs_a_decision_row(tmp_path: pathlib.Path) -> None:
    doc = decisions(fetcher={"owner": "15A", "review_only": {"transport": "Seam."}})
    assert check_decisions(api(params=[TRANSPORT]), doc, sources(tmp_path)) == [
        "api.json fetcher.transport: a ts-only param needs a decision row"
    ]
    # A row for the other language doesn't decide a TS-only param.
    doc["decisions"] = [
        {
            "factory": "fetcher",
            "option": "transport",
            "lang": "py",
            "decision": "X.",
            "owner": "15A",
        }
    ]
    assert check_decisions(api(params=[TRANSPORT]), doc, sources(tmp_path)) == [
        "api.json fetcher.transport: a ts-only param needs a decision row"
    ]
    doc["decisions"] = [
        {"factory": "fetcher", "option": "*", "lang": "ts", "decision": "All.", "owner": "15A"}
    ]
    assert check_decisions(api(params=[TRANSPORT]), doc, sources(tmp_path)) == []


def test_review_only_for_a_param_without_a_default_is_red(tmp_path: pathlib.Path) -> None:
    doc = decisions(fetcher={"owner": "15A", "review_only": {"nope": "Seam."}})
    assert check_decisions(api(), doc, sources(tmp_path)) == [
        "decisions factories.fetcher.review_only: nope has no default in spec/api.json"
    ]


def test_a_decision_for_an_unknown_factory_is_red(tmp_path: pathlib.Path) -> None:
    doc = decisions()
    doc["decisions"] = [
        {"factory": "ghost", "option": "x", "lang": "both", "decision": "No.", "owner": "15B"}
    ]
    assert check_decisions(api(), doc, sources(tmp_path)) == [
        "decisions decisions: unknown factory ghost"
    ]


def test_a_package_without_sources_for_a_language_is_red(tmp_path: pathlib.Path) -> None:
    doc = decisions({"ts": ["web.ts"]})
    assert check_decisions(api(), doc, sources(tmp_path)) == [
        "decisions packages.web: no py sources to scan"
    ]


def test_malformed_decisions_are_refused() -> None:
    doc: Obj = {
        "extra": 1,
        "packages": {"web": {"go": []}},
        "factories": {"fetcher": {"pending": "No owner."}},
        "decisions": [{"factory": "fetcher", "lang": "rust"}],
    }
    assert check_decisions(api(), doc, pathlib.Path()) == [
        "decisions: unexpected key extra",
        "decisions packages.web: expected {ts?, py?} source lists and gaps?",
        "decisions factories.fetcher: expected {owner, pending?, review_only?}",
        "decisions decisions[0]: expected ['decision', 'factory', 'lang', 'option', 'owner'], "
        "lang ts|py|both",
    ]
