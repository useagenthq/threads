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

from threads._generated import tools_v1
from threads.log import CallId, JsonObject, Spill, ToolSpec
from threads.log.digest import sha256_hex
from threads.loop.tools import Dispatched, Invocation, NotSent, Output, Uncertain
from threads.result import Ok
from threads.store import SqliteStore
from threads.tools import SandboxTools, specs
from threads.tools.specs import GATED, Writes, agent_tools

LIMITS = Spill(threshold_bytes=64, head_bytes=16, tail_bytes=8, request_budget_bytes=4096)
DAY_MS = 86_400_000
ALL = specs(
    sandbox=True,
    egress_denied=True,
    memory=Writes(),
    knowledge=True,
    framework=agent_tools(spawn=True, team=True, handoffs=True),
    gated=GATED,
    skills=True,
)
SPECS = {s.name: s for s in ALL}

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
    open_egress = {s.name: s for s in specs(sandbox=True, egress_denied=False)}
    assert open_egress["bash"].effect_class == "unguarded"
    # GUI actions unguarded, observations read_only, push reconcilable.
    classes = {n: SPECS[n].effect_class for n in ("computer", "computer_screenshot", "git_push")}
    assert classes == {
        "computer": "unguarded",
        "computer_screenshot": "read_only",
        "git_push": "reconcilable",
    }
    # Gated tools need their capability; notebook_edit comes with any sandbox.
    plain = {s.name for s in open_egress.values()}
    assert "notebook_edit" in plain
    assert not plain & GATED
    host_only = {s.name for s in specs(sandbox=False, egress_denied=True, gated=GATED)}
    assert host_only == {"read_tool_result", "todo_write", "web_fetch", "web_search"}


def test_specs_are_the_shared_catalog_and_read_tool_result_is_always_there() -> None:
    catalog: JsonValue = json.loads(tools_v1.TOOL_CATALOG)
    pinned = [
        {"name": s.name, "description": s.description, "input_schema": s.input_schema}
        for s in SPECS.values()
    ]
    assert json.loads(json.dumps(pinned)) == catalog
    bare = specs(sandbox=False, egress_denied=True)
    assert [s.name for s in bare] == ["read_tool_result", "todo_write"]


def test_memory_and_knowledge_tools_are_pinned_only_with_a_provider() -> None:
    local = {
        s.name: s
        for s in specs(
            sandbox=False,
            egress_denied=True,
            memory=Writes("idempotent", DAY_MS),
            knowledge=True,
        )
    }
    assert sorted(local) == [
        "forget_memory",
        "read_tool_result",
        "save_memory",
        "search_knowledge",
        "search_memory",
        "todo_write",
    ]
    assert local["save_memory"].dedup_window_ms == DAY_MS
    assert local["search_memory"].effect_class == "read_only"
    assert local["search_knowledge"].effect_class == "read_only"
    other = {s.name: s for s in specs(sandbox=False, egress_denied=True, memory=Writes())}
    assert other["forget_memory"].effect_class == "unguarded"
    assert "search_knowledge" not in other


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
        assert output(await call("read", {"path": "src/a.py"})).text == ("1\tx = 1\n2\ty = 2\n3\t")
        more = output(await call("read", {"path": "/workspace/src/a.py", "limit": 1}))
        assert more.text == "1\tx = 1"
        output(await call("edit", {"path": "src/a.py", "old_string": "y = 2", "new_string": "z"}))
        assert (tmp_path / "src/a.py").read_text() == "x = 1\nz\n"

    run(tmp_path, body)


@pytest.mark.parametrize(
    ("tool", "input", "error"),
    [
        ("read", {"path": "missing.txt"}, "not_found:"),
        ("edit", {"path": "a.txt", "old_string": "a", "new_string": "b"}, "old_string matches"),
        ("edit", {"path": "a.txt", "old_string": "q", "new_string": "b"}, "old_string not found"),
        (
            "write",
            {"path": "a.txt", "content": "new", "expected_sha256": "0" * 64},
            "expected_sha256",
        ),
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
        assert sorted(found.split()) == ["/workspace/pkg/a.py", "/workspace/pkg/sub/b.py"]
        hits = output(await call("grep", {"pattern": "needle", "glob": "*.py"})).text
        assert hits == "/workspace/pkg/sub/b.py:1:print('needle')"
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


def test_notebook_edit_and_read_by_cell_id(tmp_path: Path) -> None:
    cell: JsonValue = {
        "cell_type": "code",
        "id": "c1",
        "metadata": {},
        "source": "1",
        "outputs": [],
    }
    (tmp_path / "n.ipynb").write_text(json.dumps({"nbformat": 4, "cells": [cell]}))

    async def body(call: Call, _s: LocalSession, _st: SqliteStore) -> None:
        edit: JsonObject = {
            "path": "n.ipynb",
            "cell_id": "c1",
            "new_source": "# Notes",
            "cell_type": "markdown",
        }
        got = output(await call("notebook_edit", {**edit, "mode": "insert"}))
        new_id = sha256_hex(b"b:notebook_edit")[:16]
        assert got.text == f"inserted cell {new_id} after c1"
        read = output(await call("read", {"path": "n.ipynb"}))
        assert read.text == f"1\t[cell c1] code\n2\t1\n3\t[cell {new_id}] markdown\n4\t# Notes"
        missing = output(await call("notebook_edit", {**edit, "cell_id": "nope"}))
        assert missing.is_error
        assert missing.text == "cell_id nope not found; nothing written"

    run(tmp_path, body)
