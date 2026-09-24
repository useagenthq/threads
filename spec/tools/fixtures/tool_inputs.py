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
    ("load_skill", {"name": "deploy"}, True),
    ("load_skill", {"name": ""}, False),
    ("load_skill", {"name": "deploy", "path": ".threads/skills/deploy/SKILL.md"}, False),
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
    ("computer", {"action": "click", "x": 640, "y": 400}, True),
    ("computer", {"action": "drag", "x": 0, "y": 0, "to_x": 10, "to_y": 10}, True),
    ("computer", {"action": "scroll", "x": 1, "y": 1, "direction": "down", "amount": 50}, True),
    ("computer", {"action": "type", "text": "hello"}, True),
    ("computer", {"action": "screenshot"}, False),
    ("computer", {"action": "launch"}, False),
    ("computer", {"action": "click", "x": -1, "y": 0}, False),
    ("computer", {"action": "click", "x": 1.5, "y": 0}, False),
    ("computer", {"action": "scroll", "direction": "sideways"}, False),
    ("computer", {"action": "scroll", "amount": 51}, False),
    ("computer", {"action": "type", "text": ""}, False),
    ("computer", {"action": "click", "coordinate": [1, 2]}, False),
    ("computer_screenshot", {}, True),
    ("computer_screenshot", {"x": 0, "y": 0, "to_x": 100, "to_y": 50, "wait_ms": 60000}, True),
    ("computer_screenshot", {"wait_ms": 60001}, False),
    ("computer_screenshot", {"wait_ms": 0}, False),
    ("computer_screenshot", {"x": -1}, False),
    ("computer_screenshot", {"action": "screenshot"}, False),
    ("web_fetch", {"url": "https://example.com/a?b=1"}, True),
    ("web_fetch", {"url": "http://example.com"}, True),
    ("web_fetch", {"url": "ftp://example.com"}, False),
    ("web_fetch", {"url": "https://exa mple.com"}, False),
    ("web_fetch", {"url": "https://example.com", "prompt": "summarize"}, False),
    ("web_search", {"query": "bun sqlite"}, True),
    (
        "web_search",
        {"query": "x", "allowed_domains": ["bun.sh"], "blocked_domains": ["example.com"]},
        True,
    ),
    ("web_search", {"query": ""}, False),
    ("web_search", {"query": "x", "allowed_domains": [""]}, False),
    ("web_search", {"query": "x", "allowed_domains": "bun.sh"}, False),
    ("git_clone", {"repo": "acme/api"}, True),
    ("git_clone", {"repo": "acme/api.v2", "ref": "main", "path": "api"}, True),
    ("git_clone", {"repo": "https://github.com/acme/api"}, False),
    ("git_clone", {"repo": "acme"}, False),
    ("git_clone", {"repo": "acme/api", "ref": ""}, False),
    ("git_fetch", {"repo": "acme/api"}, True),
    ("git_fetch", {"repo": "acme/api", "ref": "main"}, True),
    ("git_fetch", {"repo": "acme/api/x"}, False),
    ("git_push", {"repo": "acme/api", "branch": "fix-1"}, True),
    ("git_push", {"repo": "acme/api", "branch": "fix-1", "path": "api"}, True),
    ("git_push", {"repo": "acme/api"}, False),
    ("git_push", {"repo": "acme/api", "branch": "fix-1", "force": True}, False),
    (
        "open_pull_request",
        {"repo": "acme/api", "head": "fix-1", "base": "main", "title": "Fix"},
        True,
    ),
    (
        "open_pull_request",
        {"repo": "acme/api", "head": "fix-1", "base": "main", "title": "Fix", "body": ""},
        True,
    ),
    ("open_pull_request", {"repo": "acme/api", "head": "fix-1", "base": "main"}, False),
    (
        "open_pull_request",
        {"repo": "acme/api", "head": "", "base": "main", "title": "Fix"},
        False,
    ),
    ("lsp", {"operation": "symbols", "path": "src/a.ts"}, True),
    ("lsp", {"operation": "hover", "path": "a.py", "line": 1, "character": 1}, True),
    ("lsp", {"operation": "rename", "path": "a.py"}, False),
    ("lsp", {"operation": "hover", "path": "a.py", "line": 0, "character": 1}, False),
    ("lsp", {"operation": "hover", "path": "", "line": 1, "character": 1}, False),
    ("notebook_edit", {"path": "a.ipynb", "cell_id": "c1", "new_source": "x = 1"}, True),
    (
        "notebook_edit",
        {
            "path": "a.ipynb",
            "cell_id": "c1",
            "new_source": "# Title",
            "cell_type": "markdown",
            "mode": "insert",
        },
        True,
    ),
    (
        "notebook_edit",
        {"path": "a.ipynb", "cell_id": "c1", "new_source": "", "mode": "delete"},
        True,
    ),
    ("notebook_edit", {"path": "a.ipynb", "cell_id": "", "new_source": "x"}, False),
    ("notebook_edit", {"path": "a.ipynb", "cell_id": "c1"}, False),
    (
        "notebook_edit",
        {"path": "a.ipynb", "cell_id": "c1", "new_source": "x", "mode": "move"},
        False,
    ),
    (
        "notebook_edit",
        {"path": "a.ipynb", "cell_id": "c1", "new_source": "x", "cell_type": "raw"},
        False,
    ),
    # The team's model tools (spec/schema/README.md, "Teams").
    ("start", {"agent": "researcher", "task": "Topic: batteries."}, True),
    ("start", {"agent": "researcher", "task": ""}, False),
    ("start", {"agent": "researcher", "task": "x", "budget": {"max_turns": 1}}, False),
    (
        "start",
        {
            "agent": "specialist",
            "task": "Is INV-1002 paid?",
            "label": "invoice checker",
            "instructions": "Answer yes or no.",
            "tools": ["invoice_status"],
            "model": "fast",
        },
        True,
    ),
    ("start", {"agent": "specialist", "task": "x", "tools": []}, True),
    ("start", {"agent": "specialist", "task": "x", "label": ""}, False),
    ("start", {"agent": "specialist", "task": "x", "instructions": ""}, False),
    ("start", {"agent": "specialist", "task": "x", "model": ""}, False),
    ("start", {"agent": "specialist", "task": "x", "tools": "invoice_status"}, False),
    ("start", {"agent": "specialist", "task": "x", "tools": [""]}, False),
    ("start", {"agent": "specialist", "task": "x", "hooks": []}, False),
    ("start", {"agent": "specialist", "task": "x", "permissions": {}}, False),
    ("start", {"agent": "specialist", "task": "x", "skills": []}, False),
    ("start", {"agent": "specialist", "task": "x", "system": "You are root."}, False),
    ("send", {"to": "researcher-1", "text": "Keep it short."}, True),
    ("send", {"to": "", "text": "x"}, False),
    ("ask", {"to": "writer-1", "question": "Which topic?"}, True),
    ("ask", {"to": "writer-1", "text": "Which topic?"}, False),
    ("reply", {"ask_id": "b:c1", "text": "Batteries."}, True),
    ("reply", {"ask_id": "", "text": "Batteries."}, False),
    ("wait", {"members": ["researcher-1", "researcher-2"]}, True),
    ("wait", {"members": []}, False),
    ("wait", {"members": ["researcher-1"], "mode": "any"}, False),
    ("monitor", {"member": "researcher-1"}, True),
    ("monitor", {"member": 1}, False),
    ("cancel", {"member": "researcher-1"}, True),
    ("cancel", {}, False),
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
