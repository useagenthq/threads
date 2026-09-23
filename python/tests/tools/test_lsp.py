"""The lsp tool through the in-sandbox driver, against a scripted language
server run as a real local process: every operation, 1-based positions, and a server that is
missing or undeclared answers unavailable, never an empty success."""

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from local_sandbox import LocalSession
from sandbox_kit import KitContext

from threads.log import CallId, JsonObject, Spill
from threads.loop.tools import Dispatched, Invocation, Output
from threads.result import Ok
from threads.store import SqliteStore
from threads.tools import SandboxTools, specs
from threads.tools.lsp import render

if TYPE_CHECKING:
    from pydantic import JsonValue

FAKE = str(Path(__file__).resolve().parents[1] / "fake_lsp.py")
LIMITS = Spill(
    threshold_bytes=65536, head_bytes=32768, tail_bytes=32768, request_budget_bytes=1 << 20
)
SPEC = next(
    s for s in specs(sandbox=True, egress_denied=True, gated=frozenset({"lsp"})) if s.name == "lsp"
)


def ask(tmp_path: Path, input: JsonObject, servers: Mapping[str, Sequence[str]]) -> Dispatched:
    (tmp_path / "app.py").write_text("x = undefined\n\ndef f() -> int:\n    return 1\n")

    async def main() -> Dispatched:
        opened = await SqliteStore.open()
        assert isinstance(opened, Ok)
        session = LocalSession(tmp_path)
        tools = SandboxTools(session.open, KitContext(), opened.value, lambda: LIMITS, servers)
        assert tools.invalid(SPEC, input) is None
        got = await tools.dispatch(Invocation(SPEC, CallId("call_1"), input, "b:call_1"))
        await opened.value.close()
        return got

    return asyncio.run(main())


# Bare, as a declared server usually is: the driver runs with no PATH, and python3 is in
# /usr/local/bin on python:* images, outside the default search path.
SERVERS = {"python": ("python3", FAKE)}


@pytest.mark.parametrize(
    ("input", "text"),
    [
        ({"operation": "diagnostics", "path": "app.py"}, "1:5 error: boom"),
        (
            {"operation": "definition", "path": "app.py", "line": 1, "character": 5},
            "/workspace/app.py:3:1",
        ),
        (
            {"operation": "references", "path": "app.py", "line": 3, "character": 5},
            "/workspace/app.py:3:1\n/workspace/app.py:6:5",
        ),
        ({"operation": "hover", "path": "app.py", "line": 3, "character": 5}, "def f() -> int"),
        ({"operation": "symbols", "path": "app.py"}, "f (kind 12) at 3:1\n  g (kind 12) at 4:5"),
    ],
)
def test_each_operation_answers_from_the_server(
    tmp_path: Path, input: JsonObject, text: str
) -> None:
    got = ask(tmp_path, input, SERVERS)
    assert isinstance(got, Output)
    assert not got.is_error, got.text
    assert got.text.replace(str(tmp_path.resolve()), "/workspace").replace("/private", "") == text


def test_a_missing_or_undeclared_server_is_unavailable(tmp_path: Path) -> None:
    missing = ask(
        tmp_path, {"operation": "symbols", "path": "app.py"}, {"python": ("no-such-server",)}
    )
    assert isinstance(missing, Output)
    assert missing.is_error
    assert missing.text.startswith("unavailable: the language server did not start")
    undeclared = ask(tmp_path, {"operation": "symbols", "path": "main.go"}, SERVERS)
    assert undeclared == Output("unavailable: no language server is declared for main.go", True)
    needs = ask(tmp_path, {"operation": "hover", "path": "app.py"}, SERVERS)
    assert needs == Output("hover needs line and character", True)


def test_render_handles_location_links_and_empty_answers() -> None:
    link: JsonValue = {
        "targetUri": "file:///w/a.py",
        "targetSelectionRange": {"start": {"line": 0, "character": 0}},
    }
    assert render("definition", [link]) == "/w/a.py:1:1"
    assert render("diagnostics", []) == "no diagnostics"
    assert render("hover", {"contents": ["a", {"value": "b"}]}) == "a\n\nb"
