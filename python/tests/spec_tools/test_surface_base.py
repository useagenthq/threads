"""scripts/surface-base.sh and the workflows' surface steps, run exactly as CI runs them, over
throwaway git repositories: an origin with main, and a clone holding the PR or the push."""

import os
import pathlib
import re
import shlex
import shutil
import subprocess

import pytest
from check_api import Json
from git_kit import commit, git, init, write_json
from surface_kit import PY_JUNIT, TS_JUNIT, api, coverage

REPO = pathlib.Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"
SHA_LINE = re.compile(r"^[0-9a-f]{40}\n$")
FAKE_PACKAGE = {
    "fakepkg/__init__.py": """from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class Model(Protocol):
    info: str

    def send(self) -> None: ...


@runtime_checkable
class LooksUp(Protocol):
    def lookup(self) -> None: ...


def agent(*, model: str, name: str = "") -> None: ...


def run_sync() -> None: ...


@dataclass(frozen=True)
class Skill:
    name: str
    note: str = ""


__all__ = ["LooksUp", "Model", "Skill", "agent", "run_sync"]
""",
    "fakepkg/host.py": """from typing import Protocol


class Channel(Protocol):
    def verify(self) -> None: ...


__all__ = ["Channel"]
""",
}
PYPROJECT = """[project]
name = "surface-fixture"
version = "0"
requires-python = ">=3.12"

[tool.uv]
package = false
"""


def step(workflow: str, name: str) -> str:
    """The shell text of one named step's run: key, exactly as the workflow has it."""
    text = (WORKFLOWS / workflow).read_text()
    block = text.split(f"- name: {name}\n", 1)[1].split("\n      - ", 1)[0]
    run = block.split("run: ", 1)[1]
    if not run.startswith("|"):
        return run.strip()
    return "\n".join(line.removeprefix("          ") for line in run.splitlines()[1:])


def github(script: str, event: str, base_ref: str = "", before: str = "") -> str:
    expressions = {"github.event_name": event, "github.base_ref": base_ref,
                   "github.event.before": before}  # fmt: skip
    for key, value in expressions.items():
        script = script.replace("${{ " + key + " }}", value)
    return script


def bash(cwd: pathlib.Path, script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv in a test
        ["bash", "-euo", "pipefail", "-c", script],  # noqa: S607 - bash from PATH
        cwd=cwd,
        env=os.environ | env,
        capture_output=True,
        text=True,
        check=False,
    )


class Clone:
    """origin (main) and a clone of it: the checkout CI makes with fetch-depth 0."""

    def __init__(self, tmp: pathlib.Path) -> None:
        self.origin, self.root, self.temp = tmp / "origin", tmp / "clone", tmp / "runner"
        self.temp.mkdir()
        init(self.origin)
        self.install(self.origin)
        self.base = commit(self.origin, "main before the gate")
        git(self.origin, "config", "receive.denyCurrentBranch", "updateInstead")
        git(tmp, "clone", "-q", str(self.origin), str(self.root))

    @staticmethod
    def install(repo: pathlib.Path) -> None:
        tools = repo / "spec" / "tools"
        tools.mkdir(parents=True)
        # Every tool module: the gate imports check_api, which imports the factory checks.
        for source in (REPO / "spec" / "tools").glob("*.py"):
            shutil.copy(source, tools / source.name)
        # The gate reads which factory defaults are review-only from the decisions file.
        decisions = "api-surface-factory-decisions.json"
        shutil.copy(REPO / "spec" / decisions, repo / "spec" / decisions)
        (repo / "scripts").mkdir()
        shutil.copy(REPO / "scripts" / "surface-base.sh", repo / "scripts" / "surface-base.sh")
        for path, source in FAKE_PACKAGE.items():
            (repo / "spec" / "tools" / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / "spec" / "tools" / path).write_text(source)
        (repo / "python").mkdir(exist_ok=True)
        (repo / "python" / "pyproject.toml").write_text(PYPROJECT)
        (repo / "python" / "junit.xml").write_text(PY_JUNIT)
        (repo / "typescript").mkdir(exist_ok=True)
        (repo / "typescript" / "junit.xml").write_text(TS_JUNIT)
        (repo / ".gitignore").write_text("python/.venv/\npython/uv.lock\n")
        write_json(repo / "spec" / "api.json", api())
        write_json(repo / "spec" / "api-coverage.json", coverage())

    def write(self, name: str, doc: Json) -> None:
        write_json(self.root / "spec" / name, doc)

    def resolve(self, *args: str) -> subprocess.CompletedProcess[str]:
        return bash(self.root, shlex.join(["scripts/surface-base.sh", *args]), {})

    def mode(self, workflow: str, event: str, **refs: str) -> str:
        """Runs the workflow's base step; returns the SURFACE_MODE it exported."""
        github_env = self.temp / "github_env"
        github_env.write_text("")
        env = {"RUNNER_TEMP": str(self.temp), "GITHUB_ENV": str(github_env)}
        done = bash(self.root, github(step(workflow, "API surface base"), event, **refs), env)
        assert done.returncode == 0, done.stderr
        return github_env.read_text().strip().removeprefix("SURFACE_MODE=")

    def gate(self, workflow: str, mode: str) -> subprocess.CompletedProcess[str]:
        return bash(self.root, step(workflow, "API surface gate"), {"SURFACE_MODE": mode})


@pytest.fixture
def clone(tmp_path: pathlib.Path) -> Clone:
    return Clone(tmp_path)


def test_both_workflows_resolve_the_base_the_same_way() -> None:
    assert step("typescript.yml", "API surface base") == step("python.yml", "API surface base")
    for workflow in ("typescript.yml", "python.yml"):
        assert "fetch-depth: 0" in (WORKFLOWS / workflow).read_text()


def test_a_pr_whose_base_lacks_the_gaps_file_bootstraps(clone: Clone) -> None:
    git(clone.root, "checkout", "-q", "-b", "gate")
    clone.write("api-surface-gaps.json", [])
    commit(clone.root, "install the gate")
    resolved = clone.resolve("--event", "pull_request", "--base-ref", "main", "--before", "")
    assert SHA_LINE.match(resolved.stdout)
    assert resolved.stdout.strip() == clone.base
    mode = clone.mode("typescript.yml", "pull_request", base_ref="main")
    assert mode == f"--bootstrap {clone.base}"
    assert clone.gate("typescript.yml", mode).returncode == 0
    assert clone.gate("python.yml", mode).returncode == 0


def later_pr(clone: Clone) -> str:
    """main has the gaps file; the PR branches off it. Returns the SURFACE_MODE."""
    clone.write("api-surface-gaps.json", [])
    base = commit(clone.root, "install the gate")
    git(clone.root, "push", "-q", "origin", "HEAD:main")
    git(clone.root, "checkout", "-q", "-b", "pr")
    mode = clone.mode("python.yml", "pull_request", base_ref="main")
    assert mode == (
        f"--baseline {clone.temp}/base-gaps.json --baseline-api {clone.temp}/base-api.json"
    )
    assert (clone.temp / "base-api.json").read_text() == git(
        clone.root, "show", f"{base}:spec/api.json"
    ) + "\n"
    return mode


def test_a_pr_adding_a_contract_member_with_its_gap_is_green(clone: Clone) -> None:
    mode = later_pr(clone)
    contract = api()
    functions = contract["functions"]
    assert isinstance(functions, dict)
    functions["replay"] = {"ts": "replay", "py": "replay", "async": True, "params": [],
                           "returns": {"prim": "void"}}  # fmt: skip
    clone.write("api.json", contract)
    gaps: list[Json] = [{"name": "replay", "lang": lang, "kind": "missing", "lane": "01-gate"}
            for lang in ("py", "ts")]  # fmt: skip
    clone.write("api-surface-gaps.json", gaps)
    commit(clone.root, "spec-first: replay")
    for workflow in ("typescript.yml", "python.yml"):
        done = clone.gate(workflow, mode)
        assert done.returncode == 0, done.stdout + done.stderr


def test_a_pr_removing_a_member_and_listing_its_gap_is_red(clone: Clone) -> None:
    mode = later_pr(clone)
    package = clone.root / "spec" / "tools" / "fakepkg" / "__init__.py"
    package.write_text(package.read_text().replace('"run_sync"]', "]"))
    dropped: Json = {
        "name": "runSync",
        "lang": "py",
        "kind": "missing",
        "lane": "01-gate",
    }
    clone.write("api-surface-gaps.json", [dropped])
    clone.write("api-coverage.json", {k: v for k, v in coverage().items() if k != "runSync"})
    commit(clone.root, "drop run_sync")
    done = clone.gate("python.yml", mode)
    assert done.returncode == 1
    assert "new gap runSync (py, missing, lane 01-gate) for a member that exists" in done.stdout


def test_a_push_resolves_to_its_before_sha(clone: Clone) -> None:
    commit(clone.root, "pushed")
    done = clone.resolve("--event", "push", "--base-ref", "", "--before", clone.base)
    assert done.stdout == f"{clone.base}\n"


def test_an_all_zero_before_falls_back_to_the_merge_base_with_main(clone: Clone) -> None:
    commit(clone.root, "first push of a new branch")
    done = clone.resolve("--event", "push", "--before", "0" * 40)
    assert done.stdout == f"{clone.base}\n"


def test_a_shallow_clone_without_the_base_fails_with_the_message(tmp_path: pathlib.Path) -> None:
    clone = Clone(tmp_path)
    commit(clone.origin, "a later commit on main")
    shallow = tmp_path / "shallow"
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{clone.origin}", str(shallow))
    done = bash(shallow, f"scripts/surface-base.sh --event push --before {clone.base}", {})
    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr == (
        f"surface gate: can't resolve the base commit (event push, base ref , before "
        f"{clone.base}); fetch history or check the workflow checkout\n"
    )
