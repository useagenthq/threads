"""The built-in sandbox tools against real local processes: typed results,
output spilled at the source, a deadline that is uncertainty, and an empty environment."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from local_sandbox import LocalSession
from pydantic import JsonValue
from sandbox_kit import KitContext

from threads.log import CallId, JsonObject, Spill, ToolSpec
from threads.log.digest import sha256_hex
from threads.loop.tools import Dispatched, Invocation, NotSent, Output, Uncertain
from threads.result import Ok
from threads.store import SqliteStore
from threads.tools import SandboxTools, specs

LIMITS = Spill(threshold_bytes=64, head_bytes=16, tail_bytes=8, request_budget_bytes=4096)
SPECS = {s.name: s for s in specs(egress_denied=True)}

type Call = Callable[[str, JsonObject], Awaitable[Dispatched]]


def run(tmp_path: Path, body: Callable[[Call, LocalSession, SqliteStore], Awaitable[None]]) -> None:
    async def main() -> None:
        opened = await SqliteStore.open()
        assert isinstance(opened, Ok)
        session = LocalSession(tmp_path)
        tools = SandboxTools(session.open, KitContext(), opened.value, lambda: LIMITS)

        async def call(name: str, input: JsonObject) -> Dispatched:
            spec: ToolSpec = SPECS[name]
            assert tools.invalid(spec, input) is None
            return await tools.dispatch(Invocation(spec, CallId("call_1"), input, f"b:{name}"))

        await body(call, session, opened.value)
        await opened.value.close()

    asyncio.run(main())


def output(got: Dispatched) -> Output:
    assert isinstance(got, Output), got
    return got


def test_specs_are_sorted_and_bash_is_unguarded_without_deny_all_egress() -> None:
    assert list(SPECS) == sorted(SPECS)
    assert SPECS["bash"].effect_class == "sandbox_local"
    assert SPECS["read"].effect_class == "read_only"
    open_egress = {s.name: s for s in specs(egress_denied=False)}
    assert open_egress["bash"].effect_class == "unguarded"


def test_bash_returns_exit_code_and_streams_as_json(tmp_path: Path) -> None:
    async def body(call: Call, _s: LocalSession, _st: SqliteStore) -> None:
        ok = output(await call("bash", {"command": "echo hi; echo err >&2"}))
        assert json.loads(ok.text) == {
            "exit_code": 0,
            "stdout": "hi\n",
            "stderr": "err\n",
            "truncated": False,
        }
        assert not ok.is_error
        failed = output(await call("bash", {"command": "exit 3"}))
        assert failed.is_error

    run(tmp_path, body)


def test_bash_spills_full_output_and_keeps_previews(tmp_path: Path) -> None:
    async def body(call: Call, _s: LocalSession, store: SqliteStore) -> None:
        got = output(await call("bash", {"command": "seq 1 500"}))
        parsed = json.loads(got.text)
        assert parsed["truncated"] is True
        assert parsed["stdout"].startswith("1\n2\n")
        assert got.full_output is not None
        full = await store.get_artifact(got.full_output.sha256)
        assert isinstance(full, Ok)
        assert full.value == "".join(f"{n}\n" for n in range(1, 501)).encode()

    run(tmp_path, body)


def test_a_bash_deadline_is_uncertain_and_the_process_group_is_killed(tmp_path: Path) -> None:
    async def body(call: Call, session: LocalSession, _st: SqliteStore) -> None:
        got = await call("bash", {"command": "sleep 30", "timeout_ms": 50})
        assert got == Uncertain("timeout")
        await asyncio.sleep(0.05)
        proc = session.procs["b:bash"]
        assert proc.returncode is not None

    run(tmp_path, body)


def test_commands_run_with_an_empty_environment(tmp_path: Path) -> None:
    async def body(call: Call, session: LocalSession, _st: SqliteStore) -> None:
        got = output(await call("bash", {"command": "env"}))
        assert "HOME=" not in got.text
        assert session.envs == [{}]

    run(tmp_path, body)


def test_read_write_and_edit(tmp_path: Path) -> None:
    async def body(call: Call, _s: LocalSession, _st: SqliteStore) -> None:
        output(await call("write", {"path": "src/a.py", "content": "x = 1\ny = 2\n"}))
        assert output(await call("read", {"path": "src/a.py"})).text == (
            "     1\tx = 1\n     2\ty = 2\n"
        )
        more = output(await call("read", {"path": "/workspace/src/a.py", "limit": 1}))
        assert more.text.endswith("[1 more lines]\n")
        output(await call("edit", {"path": "src/a.py", "old_string": "y = 2", "new_string": "z"}))
        assert (tmp_path / "src/a.py").read_text() == "x = 1\nz\n"

    run(tmp_path, body)


@pytest.mark.parametrize(
    ("tool", "input", "error"),
    [
        ("read", {"path": "missing.txt"}, "not_found:"),
        ("edit", {"path": "a.txt", "old_string": "a", "new_string": "b"}, "ambiguous:"),
        ("edit", {"path": "a.txt", "old_string": "q", "new_string": "b"}, "not_found:"),
        ("write", {"path": "a.txt", "content": "new", "expected_sha256": "0" * 64}, "conflict:"),
    ],
)
def test_file_failures_are_typed_and_change_nothing(
    tmp_path: Path, tool: str, input: dict[str, JsonValue], error: str
) -> None:
    (tmp_path / "a.txt").write_text("a a")

    async def body(call: Call, _s: LocalSession, _st: SqliteStore) -> None:
        got = output(await call(tool, input))
        assert got.is_error
        assert got.text.startswith(error)
        assert (tmp_path / "a.txt").read_text() == "a a"

    run(tmp_path, body)


def test_a_matching_expected_sha256_writes(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("old")

    async def body(call: Call, _s: LocalSession, _st: SqliteStore) -> None:
        digest = sha256_hex(b"old")
        input: JsonObject = {"path": "a.txt", "content": "new", "expected_sha256": digest}
        assert not output(await call("write", input)).is_error
        assert (tmp_path / "a.txt").read_text() == "new"

    run(tmp_path, body)


def test_ls_glob_and_grep(tmp_path: Path) -> None:
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "a.py").write_text("import os\n")
    (tmp_path / "pkg" / "sub" / "b.py").write_text("print('needle')\n")
    (tmp_path / "pkg" / "c.txt").write_text("needle\n")

    async def body(call: Call, _s: LocalSession, _st: SqliteStore) -> None:
        assert output(await call("ls", {"path": "pkg"})).text == "a.py\nc.txt\nsub/\n"
        found = output(await call("glob", {"pattern": "**/*.py", "path": "pkg"})).text
        assert sorted(found.split()) == ["pkg/a.py", "pkg/sub/b.py"]
        hits = output(await call("grep", {"pattern": "needle", "glob": "*.py"})).text
        assert hits == "pkg/sub/b.py:1:print('needle')\n"
        none = output(await call("grep", {"pattern": "absent"}))
        assert (none.text, none.is_error) == ("no matches", False)

    run(tmp_path, body)


def test_invalid_arguments_fail_before_dispatch(tmp_path: Path) -> None:
    async def main() -> None:
        opened = await SqliteStore.open()
        assert isinstance(opened, Ok)
        tools = SandboxTools(
            LocalSession(tmp_path).open, KitContext(), opened.value, lambda: LIMITS
        )
        assert tools.invalid(SPECS["bash"], {"command": 1}) is not None
        assert tools.invalid(SPECS["read"], {"path": "a", "extra": True}) is not None
        await opened.value.close()

    asyncio.run(main())


def test_a_stale_owner_sends_nothing(tmp_path: Path) -> None:
    async def main() -> None:
        opened = await SqliteStore.open()
        assert isinstance(opened, Ok)
        session = LocalSession(tmp_path)
        tools = SandboxTools(session.open, KitContext(live=False), opened.value, lambda: LIMITS)
        call = Invocation(SPECS["bash"], CallId("call_1"), {"command": "touch x"}, "b:c")
        assert await tools.dispatch(call) == NotSent()
        assert not (tmp_path / "x").exists()
        await opened.value.close()

    asyncio.run(main())
