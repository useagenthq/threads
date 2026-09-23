# pyright: strict
"""The built-in tool input vector: accept/reject cases for every catalog input, and
the SHA-256 of the canonical catalog bytes (spec/schema/tools.v1.catalog.json) both runtimes
pin. The decisions are authored from the catalog rules, never read from an implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import CASES, sha
from .pieces import dump

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

VECTOR = CASES.parent / "vectors" / "tool-inputs.json"
CATALOG = CASES.parents[1] / "schema" / "tools.v1.catalog.json"
SHA = "0" * 64
UPPER = "A" * 64

# (tool, input, valid)
CASES_: tuple[tuple[str, Obj, bool], ...] = (
    ("bash", {"command": "ls"}, True),
    ("bash", {"command": "ls", "timeout_ms": 1}, True),
    ("bash", {"command": ""}, False),
    ("bash", {"command": "ls", "timeout_ms": 0}, False),
    ("bash", {"command": "ls", "timeout_ms": None}, False),
    ("bash", {"command": "ls", "timeout_ms": 1.5}, False),
    ("bash", {"command": "ls", "timeout_ms": "10"}, False),
    ("bash", {"command": "ls", "shell": "zsh"}, False),
    ("read", {"path": "a.txt"}, True),
    ("read", {"path": "a.txt", "offset": 1, "limit": 1}, True),
    ("read", {"path": "a.txt", "offset": 0}, False),
    ("read", {"path": "a.txt", "limit": 0}, False),
    ("read", {"path": "a.txt", "offset": "1"}, False),
    ("read", {"path": ""}, False),
    ("read", {}, False),
    ("write", {"path": "a.txt", "content": ""}, True),
    ("write", {"path": "a.txt", "content": "x", "expected_sha256": SHA}, True),
    ("write", {"path": "a.txt", "content": "x", "expected_sha256": UPPER}, False),
    ("write", {"path": "a.txt", "content": "x", "expected_sha256": SHA[1:]}, False),
    ("write", {"path": "a.txt", "content": "x", "expected_sha256": None}, False),
    ("write", {"path": "a.txt"}, False),
    ("edit", {"path": "a.txt", "old_string": "a", "new_string": ""}, True),
    (
        "edit",
        {"path": "a", "old_string": "a", "new_string": "b", "replace_all": True},
        True,
    ),
    ("edit", {"path": "a.txt", "old_string": "", "new_string": "b"}, False),
    (
        "edit",
        {"path": "a", "old_string": "a", "new_string": "b", "replace_all": "true"},
        False,
    ),
    ("ls", {}, True),
    ("ls", {"path": "src"}, True),
    ("ls", {"path": None}, False),
    ("ls", {"path": ""}, False),
    ("glob", {"pattern": "**/*.ts"}, True),
    ("glob", {"pattern": "*.ts", "path": "src"}, True),
    ("glob", {"pattern": ""}, False),
    ("glob", {"pattern": "*.ts", "path": None}, False),
    ("grep", {"pattern": "TODO"}, True),
    ("grep", {"pattern": "TODO", "path": "src", "glob": "*.ts"}, True),
    ("grep", {"pattern": "TODO", "glob": None}, False),
    ("grep", {"pattern": "TODO", "glob": ""}, False),
    ("grep", {"pattern": ""}, False),
    ("read_tool_result", {"call_id": "c1", "offset": 0, "length": 65536}, True),
    ("read_tool_result", {"call_id": "c1", "offset": 0, "length": 65537}, False),
    ("read_tool_result", {"call_id": "c1", "offset": -1, "length": 1}, False),
    ("read_tool_result", {"call_id": "", "offset": 0, "length": 1}, False),
    ("save_memory", {"text": "prefers tabs"}, True),
    ("save_memory", {"text": ""}, False),
    ("save_memory", {"text": "x", "origin": "user"}, False),
    ("search_memory", {"query": "tabs"}, True),
    ("search_memory", {"query": "tabs", "k": 20}, True),
    ("search_memory", {"query": "tabs", "k": 21}, False),
    ("search_memory", {"query": "tabs", "k": 0}, False),
    ("search_memory", {"query": "tabs", "scope": "other"}, False),
    ("forget_memory", {"id": "m1"}, True),
    ("forget_memory", {"id": ""}, False),
    ("search_knowledge", {"query": "refund"}, True),
    ("search_knowledge", {"query": "refund", "k": 3, "sources": ["handbook"]}, True),
    ("search_knowledge", {"query": "refund", "sources": [""]}, False),
    ("search_knowledge", {"query": "refund", "tenant_id": "b"}, False),
    ("search_knowledge", {"query": ""}, False),
    ("todo_write", {"todos": []}, True),
    ("todo_write", {"todos": [{"id": "1", "content": "Fix it", "status": "pending"}]}, True),
    (
        "todo_write",
        {"todos": [{"id": "1", "content": "Fix", "status": "pending", "active_form": "Fixing"}]},
        True,
    ),
    ("todo_write", {"todos": [{"id": "1", "content": "Fix", "status": "done"}]}, False),
    ("todo_write", {"todos": [{"id": "", "content": "Fix", "status": "pending"}]}, False),
    ("todo_write", {"todos": [{"id": "1", "status": "pending"}]}, False),
    ("todo_write", {"todos": "1. Fix"}, False),
    ("spawn_agent", {"agent": "reviewer", "prompt": "Review the diff."}, True),
    (
        "spawn_agent",
        {"agent": "scanner", "prompt": "Scan.", "background": True, "isolation": "none"},
        True,
    ),
    ("spawn_agent", {"agent": "reviewer", "prompt": ""}, False),
    ("spawn_agent", {"agent": "reviewer", "prompt": "x", "isolation": "vm"}, False),
    ("spawn_agent", {"agent": "reviewer", "prompt": "x", "background": "yes"}, False),
    ("handoff", {"agent": "billing"}, True),
    ("handoff", {"agent": ""}, False),
    ("handoff", {"agent": "billing", "forwarded": "none"}, False),
    ("send_message", {"to": "*", "text": "Schema is up."}, True),
    ("send_message", {"to": "bob", "text": ""}, False),
    ("team_task_create", {"subject": "Write the runner", "blocked_by": ["t1"]}, True),
    ("team_task_create", {"subject": "Write the schema", "description": "v1"}, True),
    ("team_task_create", {"subject": ""}, False),
    ("team_task_create", {"subject": "x", "task_id": "t9"}, False),
    ("team_task_claim", {"task_id": "t1"}, True),
    ("team_task_claim", {}, False),
    ("team_task_update", {"task_id": "t1", "status": "completed"}, True),
    ("team_task_update", {"task_id": "t1", "status": "released"}, True),
    ("team_task_update", {"task_id": "t1", "status": "claimed"}, False),
)


def _vector() -> str:
    cases: list[JsonValue] = [{"tool": t, "input": i, "valid": v} for t, i, v in CASES_]
    doc: Obj = {
        "description": (
            "Built-in tool inputs: each case is parsed with the tool's catalog "
            "schema (Zod in TypeScript, the generated Pydantic model in Python) and must be "
            "accepted exactly when valid. catalog_sha256 is the SHA-256 of "
            "spec/schema/tools.v1.catalog.json, the canonical catalog both runtimes pin."
        ),
        "catalog_sha256": sha(CATALOG.read_bytes()),
        "cases": cases,
    }
    return dump(doc)


def write() -> None:
    VECTOR.write_text(_vector(), encoding="utf-8")


def check() -> list[str]:
    fresh = _vector()
    current = VECTOR.read_text(encoding="utf-8") if VECTOR.exists() else ""
    return [] if fresh == current else [f"{VECTOR.name}: differs; run gen_fixtures.py"]
