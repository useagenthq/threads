"""spec/conformance/vectors/workspace-exclude.json: the deny-list and .git by segments alone, so
a local_dir pins the same tree in both languages."""

import json
from pathlib import Path

import pytest

from threads.workspace.exclude import excluded

VECTORS = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "vectors"
CASES = json.loads((VECTORS / "workspace-exclude.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["path"] for c in CASES])
def test_deny_list_vector(case: dict[str, object]) -> None:
    assert excluded(str(case["path"])) is case["excluded"]
