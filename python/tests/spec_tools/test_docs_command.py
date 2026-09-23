"""The docs generator runs as docs CI runs it, and its pages show nested docs as rows."""

import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from api_ref.text import clean, mdx

ROOT = Path(__file__).resolve().parents[3]
REFERENCE = Path("docs/content/docs/reference/functions")


def check_gen_command() -> list[str]:
    """The first command of `bun run check:gen`, run with this interpreter."""
    scripts = json.loads((ROOT / "docs" / "package.json").read_text())["scripts"]
    first = shlex.split(scripts["check:gen"].split("&&")[0])
    assert first[0] == "python3"
    return [sys.executable, *first[1:]]


def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)  # noqa: S603


def test_the_check_gen_command_passes_on_the_committed_reference() -> None:
    result = run(check_gen_command(), ROOT / "docs")
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_module_form_passes_too() -> None:
    result = run([sys.executable, "-m", "docs.scripts.gen_api_ref", "--check"], ROOT)
    assert result.returncode == 0, result.stdout + result.stderr


def api_doc(function: str, *path: str) -> str:
    """A doc from spec/api.json as a page prints it: an option of a function, then inline fields
    or callback params."""
    api = json.loads((ROOT / "spec" / "api.json").read_text())
    params = api["functions"][function]["params"]
    node = next(p for p in params if p["name"] == path[0])
    for name in path[1:]:
        t = node["type"]
        node = (
            t["object"][name]
            if "object" in t
            else next(p for p in t["fn"]["params"] if p["name"] == name)
        )
    return mdx(clean(node["doc"]))


def test_regenerated_pages_show_nested_docs_as_rows(tmp_path: Path) -> None:
    ignore = shutil.ignore_patterns("__pycache__")
    shutil.copytree(ROOT / "spec", tmp_path / "spec", ignore=ignore)
    shutil.copytree(ROOT / "docs" / "scripts", tmp_path / "docs" / "scripts", ignore=ignore)
    command = [c for c in check_gen_command() if c != "--check"]
    result = run(command, tmp_path / "docs")
    assert result.returncode == 0, result.stdout + result.stderr
    agent = (tmp_path / REFERENCE / "agent.mdx").read_text()
    for name in ("fetch", "search"):
        assert f'<Field name="web.{name}"' in agent
        assert f"  {api_doc('agent', 'web', name)}\n</Field>" in agent
    tool = (tmp_path / REFERENCE / "tool.mdx").read_text()
    for name in ("input", "ctx"):
        assert f'<Field name="execute({name})"' in tool
        assert f"  {api_doc('tool', 'execute', name)}" in tool
