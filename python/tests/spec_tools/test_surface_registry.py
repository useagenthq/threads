"""check_surface.py over a fixture repository: the gaps registry, its ratchet against the base
commit, test evidence from JUnit, and malformed registry input."""

import pathlib

import check_surface
import pytest
from check_api import Json
from git_kit import commit, git, init, write_json
from surface_contract import members, parse_gaps
from surface_coverage import junit_results
from surface_kit import PY_JUNIT, RUNS, TS_JUNIT, api, core_members, coverage, installed


def gap(name: str, lang: str = "py", kind: str = "missing") -> dict[str, Json]:
    return {"name": name, "lang": lang, "kind": kind, "lane": "unassigned"}


def with_entry(name: str, entry: Json) -> dict[str, Json]:
    out = coverage()
    out[name] = entry
    return out


class Repo:
    """A fixture repository holding the gate's inputs; the gate runs over it."""

    def __init__(self, root: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        init(root)
        paths = {"ROOT": "", "API": "api.json", "GAPS": "api-surface-gaps.json"}
        for name, path in (paths | {"COVERAGE": "api-coverage.json"}).items():
            monkeypatch.setattr(check_surface, name, root / "spec" / path if path else root)
        (root / "ts.xml").write_text(TS_JUNIT)
        (root / "py.xml").write_text(PY_JUNIT)
        self.write("api.json", api())
        self.write("api-coverage.json", coverage())

    def write(self, name: str, doc: Json) -> None:
        write_json(self.root / "spec" / name, doc)

    def gate(self, lang: str, *mode: str, core: dict[str, object] | None = None) -> list[str]:
        argv = ["--lang", lang, "--junit", str(self.root / f"{lang}.xml"), *mode]
        with installed(core):
            return check_surface.run(check_surface.parse(argv))

    def baseline(self, sha: str) -> tuple[str, ...]:
        """The workflow's baseline arguments: the base commit's gaps file and api.json."""
        paths: list[str] = []
        for name in ("api-surface-gaps.json", "api.json"):
            path = self.root / f"base-{name}"
            path.write_text(git(self.root, "show", f"{sha}:spec/{name}") + "\n")
            paths.append(str(path))
        return ("--baseline", paths[0], "--baseline-api", paths[1])


@pytest.fixture
def repo(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Repo:
    return Repo(tmp_path / "repo", monkeypatch)


def without_run_sync() -> dict[str, object]:
    return {k: v for k, v in core_members().items() if k != "run_sync"}


def no_py_run_sync_coverage(repo: Repo) -> None:
    repo.write("api-coverage.json", {k: v for k, v in coverage().items() if k != "runSync"})


def test_bootstrap_pr_accepts_true_gaps(repo: Repo) -> None:
    base = commit(repo.root, "base without a gaps file")
    repo.write("api-surface-gaps.json", [gap("runSync")])
    no_py_run_sync_coverage(repo)
    assert repo.gate("py", "--bootstrap", base, core=without_run_sync()) == []


def test_bootstrap_pr_rejects_a_listed_member_that_exists(repo: Repo) -> None:
    base = commit(repo.root, "base without a gaps file")
    repo.write("api-surface-gaps.json", [gap("runSync")])
    assert "is listed but fixed; delete its entry" in repo.gate("py", "--bootstrap", base)[0]


def test_bootstrap_refused_when_base_has_file(repo: Repo) -> None:
    repo.write("api-surface-gaps.json", [])
    base = commit(repo.root, "base with a gaps file")
    assert repo.gate("ts", "--bootstrap", base) == [
        f"surface gate: --bootstrap refused: {base} already has spec/api-surface-gaps.json"
    ]


def test_later_pr_new_gap_for_existing_member_fails(repo: Repo) -> None:
    repo.write("api-surface-gaps.json", [])
    base = commit(repo.root, "base")
    repo.write("api-surface-gaps.json", [gap("runSync")])
    no_py_run_sync_coverage(repo)
    assert repo.gate("py", *repo.baseline(base), core=without_run_sync()) == [
        "surface gate: new gap runSync (py, missing, lane unassigned) for a member that exists "
        "at the base; restore the member instead of listing it"
    ]


def test_later_pr_new_gap_for_new_member_passes(repo: Repo) -> None:
    repo.write("api-surface-gaps.json", [])
    base = commit(repo.root, "base")
    contract = api()
    functions = contract["functions"]
    assert isinstance(functions, dict)
    functions["replay"] = {"ts": "replay", "py": "replay", "async": True, "params": [],
                           "returns": {"prim": "void"}}  # fmt: skip
    repo.write("api.json", contract)
    repo.write("api-surface-gaps.json", [gap("replay"), gap("replay", "ts")])
    assert repo.gate("py", *repo.baseline(base)) == []
    assert repo.gate("ts", *repo.baseline(base)) == []


def test_baseline_without_base_api_is_refused(repo: Repo) -> None:
    repo.write("api-surface-gaps.json", [])
    assert repo.gate("ts", "--baseline", str(repo.root / "spec" / "api-surface-gaps.json")) == [
        "surface gate: --baseline needs --baseline-api (the base commit's api.json)"
    ]


def test_a_mode_is_required(repo: Repo) -> None:
    assert repo.gate("ts") == [
        "surface gate: pass exactly one of --bootstrap BASE_SHA or --baseline GAPS"
    ]


def test_release_with_gaps_fails(repo: Repo) -> None:
    base = commit(repo.root, "base")
    repo.write("api-surface-gaps.json", [gap("runSync")])
    no_py_run_sync_coverage(repo)
    assert repo.gate("py", "--bootstrap", base, "--release", core=without_run_sync()) == [
        "surface gate: release with an open gap: runSync (py, missing, lane unassigned)"
    ]


def test_coverage_id_absent_from_junit_fails(repo: Repo) -> None:
    base = commit(repo.root, "base")
    repo.write("api-surface-gaps.json", [])
    repo.write("api-coverage.json", with_entry("agent", {"ts": ["packages/a.test.ts > gone"]}))
    assert repo.gate("ts", "--bootstrap", base) == [
        "api-coverage.json agent.ts: test 'packages/a.test.ts > gone' absent from the JUnit report"
    ]


def test_coverage_id_failed_or_skipped_fails(repo: Repo) -> None:
    base = commit(repo.root, "base")
    repo.write("api-surface-gaps.json", [])
    ids: list[Json] = ["packages/a.test.ts > agent > skips", "packages/a.test.ts > breaks"]
    repo.write("api-coverage.json", with_entry("agent", {"ts": ids}))
    assert repo.gate("ts", "--bootstrap", base) == [
        "api-coverage.json agent.ts: test 'packages/a.test.ts > agent > skips' skipped",
        "api-coverage.json agent.ts: test 'packages/a.test.ts > breaks' failure",
    ]


def test_required_option_without_coverage_fails(repo: Repo) -> None:
    base = commit(repo.root, "base")
    repo.write("api-surface-gaps.json", [])
    repo.write("api-coverage.json", {k: v for k, v in coverage().items() if k != "agent.model"})
    assert repo.gate("ts", "--bootstrap", base) == [
        "api-coverage.json: agent.model has no ts test; add the ID of a test that exercises it"
    ]


def test_coverage_for_a_member_a_language_lacks_fails(repo: Repo) -> None:
    base = commit(repo.root, "base")
    repo.write("api-surface-gaps.json", [])
    repo.write("api-coverage.json", with_entry("runSync", RUNS))
    assert repo.gate("ts", "--bootstrap", base) == [
        "api-coverage.json runSync.ts: not a function, required method or required option that "
        "exists in ts; delete the entry"
    ]


@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        (gap("agent", lang="go"), "agent does not exist in go"),
        (gap("agent", kind="absent"), "kind 'absent' does not apply to function agent"),
        (gap("nope"), "nope is not in spec/api.json"),
        (gap("agent", kind="placement") | {"at": "host"}, "kind 'placement' does not apply to"),
        (gap("Model.send", "ts", "placement") | {"at": "host"}, "does not apply to method"),
        (gap("Channel", "ts", "placement"), "plus at for a placement"),
        (gap("Channel", "ts", "placement") | {"at": "mars"}, "at 'mars' is not another package"),
        (gap("Channel", "ts", "placement") | {"at": "host"}, "at 'host' is not another package"),
        (gap("agent") | {"why": "x"}, "a gap is exactly {name, lang, kind, lane}, plus at"),
        (gap("agent") | {"lane": "later"}, "lane 'later' is not NN-name or unassigned"),
    ],
)
def test_malformed_gap_entries_are_rejected(entry: Json, problem: str) -> None:
    gaps, errs = parse_gaps([entry], members(api()), "gaps")
    assert gaps == []
    assert problem in errs[0]


def test_a_duplicate_gap_is_rejected() -> None:
    errs = parse_gaps([gap("agent"), gap("agent")], members(api()), "gaps")[1]
    assert errs == ["gaps[1]: duplicate gap agent (py, missing)"]


@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        ({"ts": []}, "api-coverage.json agent.ts: expected a non-empty list of test IDs"),
        ({"ts": [""]}, "api-coverage.json agent.ts: expected a non-empty list of test IDs"),
        ({"go": ["x"]}, "api-coverage.json agent: expected {ts?, py?} lists of test IDs"),
        (["x"], "api-coverage.json agent: expected {ts?, py?} lists of test IDs"),
        ({}, "api-coverage.json agent: expected {ts?, py?} lists of test IDs"),
    ],
)
def test_malformed_coverage_entries_are_rejected(repo: Repo, entry: Json, problem: str) -> None:
    base = commit(repo.root, "base")
    repo.write("api-surface-gaps.json", [])
    repo.write("api-coverage.json", with_entry("agent", entry))
    assert problem in repo.gate("ts", "--bootstrap", base)


@pytest.mark.parametrize("name", ["agent.name", "nope"])
def test_coverage_for_a_name_that_needs_none_is_rejected_in_every_job(
    repo: Repo, name: str
) -> None:
    base = commit(repo.root, "base")
    repo.write("api-surface-gaps.json", [])
    repo.write("api-coverage.json", with_entry(name, {"py": RUNS["py"]}))
    problem = (
        f"api-coverage.json {name}: not a function, required method or required option in "
        "spec/api.json; delete the entry"
    )
    assert problem in repo.gate("ts", "--bootstrap", base)


def test_truncated_junit_is_rejected(tmp_path: pathlib.Path) -> None:
    report = tmp_path / "junit.xml"
    report.write_text(TS_JUNIT[:80])
    results, errs = junit_results(report, "ts")
    assert results == {}
    assert errs[0].startswith(f"surface gate: can't read the JUnit report {report}:")


def test_junit_ids_follow_bun_nesting_and_pytest_classes(tmp_path: pathlib.Path) -> None:
    (tmp_path / "ts.xml").write_text(TS_JUNIT)
    (tmp_path / "py.xml").write_text(PY_JUNIT)
    assert junit_results(tmp_path / "ts.xml", "ts")[0] == {
        "packages/a.test.ts > agent > runs": "passed",
        "packages/a.test.ts > agent > skips": "skipped",
        "packages/a.test.ts > sends": "passed",
        "packages/a.test.ts > breaks": "failure",
    }
    assert set(junit_results(tmp_path / "py.xml", "py")[0]) == {
        "tests.test_a::test_runs",
        "tests.test_a.TestModel::test_sends",
    }
