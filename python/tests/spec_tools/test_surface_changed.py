"""surface_changed.py: a `changed` gap needs its marker on the member's docs page, and a new one
needs a contract that actually changed since the base commit."""

import pathlib

import pytest
import surface_changed
from check_api import Json
from surface_changed import check_changed, marker
from surface_contract import Gap

LANE = "29-teams-phase2-29f"
CANCEL: Json = {"ts": "cancel", "py": "cancel", "returns": {"result": {"prim": "void"}}}
API: Json = {"types": {"Thread": {"kind": "interface", "methods": {"cancel": CANCEL}}}}
CHANGED = Gap("Thread.cancel", "ts", "changed", LANE)


@pytest.fixture
def docs(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    monkeypatch.setattr(surface_changed, "DOCS", tmp_path)
    (tmp_path / "types").mkdir()
    return tmp_path / "types" / "Thread.mdx"


def test_a_changed_member_without_its_docs_marker_fails(docs: pathlib.Path) -> None:
    docs.write_text("### cancel\n\nDurable cancel_requested.\n")
    errs = check_changed([CHANGED], API, None)
    assert len(errs) == 1
    assert "lacks its marker" in errs[0]


def test_a_changed_member_with_its_marker_passes(docs: pathlib.Path) -> None:
    docs.write_text(f"### cancel\n\n{marker(LANE)}: Until then branch_busy.\n")
    assert check_changed([CHANGED], API, None) == []


def test_a_new_changed_gap_needs_a_changed_contract(docs: pathlib.Path) -> None:
    docs.write_text(f"{marker(LANE)}: Until then branch_busy.\n")
    errs = check_changed([CHANGED], API, (API, []))
    assert len(errs) == 1
    assert "entry is the base commit's" in errs[0]
    before: Json = {"types": {"Thread": {"kind": "interface", "methods": {"cancel": {}}}}}
    assert check_changed([CHANGED], API, (before, [])) == []


def test_a_changed_gap_the_base_already_listed_passes(docs: pathlib.Path) -> None:
    docs.write_text(f"{marker(LANE)}: Until then branch_busy.\n")
    assert check_changed([CHANGED], API, (API, [CHANGED])) == []
