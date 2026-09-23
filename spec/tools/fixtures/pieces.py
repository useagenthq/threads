# pyright: strict
"""Case writing and shared fixture pieces."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, TypedDict, Unpack

from .common import (
    ADAPTER,
    ALICE,
    ALLOW,
    DAY,
    MODEL,
    NOW,
    PARAMS,
    sha,
    text,
    tokens,
    tool,
)
from .jcs import JsonValue, Obj, canonical
from .log import Log, reduce
from .render import render

if TYPE_CHECKING:
    import pathlib


class CallOpts(TypedDict, total=False):
    usage: Obj
    call_id: str
    permission: Obj


RAW_ARITY = 3


# ---------- case writing ----------
def dump(o: JsonValue) -> str:
    return json.dumps(o, indent=2, ensure_ascii=False) + "\n"


def write_case(
    root: pathlib.Path,
    case: Obj,
    log: Log | None,
    expected: Obj,
    extra: dict[str, JsonValue | bytes] | None = None,
) -> None:
    """Writes a case directory. An extra "log.jsonl" (bytes) replaces the log's export."""
    d = root / text(case["name"])
    d.mkdir(parents=True)
    (d / "case.json").write_text(dump(case))
    (d / "expected.json").write_text(dump(expected))
    if log is not None:
        (d / "log.jsonl").write_bytes(log.export())
        if log.artifacts:
            (d / "artifacts").mkdir()
            for h, b in sorted(log.artifacts.items()):
                (d / "artifacts" / h).write_bytes(b)
    for fname, content in (extra or {}).items():
        p = d / fname
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(dump(content))


def case(
    name: str,
    family: str,
    kind: str,
    description: str,
    now: int = NOW,
    **more: JsonValue,
) -> Obj:
    return {
        "name": name,
        "family": family,
        "kind": kind,
        "description": description,
        "clock": {"now": now},
        **more,
    }


# ---------- shared fixture pieces ----------
READ_FILE = tool(
    "read_file",
    "Read a file from the sandbox workspace.",
    {"path": {"type": "string"}},
    "read_only",
)
EMAIL = tool(
    "send_email",
    "Send an email.",
    {"to": {"type": "string"}, "body": {"type": "string"}},
    "unguarded",
)
CHARGE = tool(
    "charge_card",
    "Charge a customer card.",
    {"amount_cents": {"type": "integer"}, "customer": {"type": "string"}},
    "idempotent",
    DAY,
)
REFUND = tool("refund_card", "Refund a charge.", {"charge": {"type": "string"}}, "reconcilable")
SHELL = tool(
    "run_shell",
    "Run a shell command in the sandbox.",
    {"command": {"type": "string"}},
    "sandbox_local",
)
SEARCH = tool("search_web", "Search the web.", {"q": {"type": "string"}}, "unguarded")
TESTS = tool("run_tests", "Run the test suite (non-blocking).", {}, "read_only")
EMAIL_IN: Obj = {"body": "The build is green.", "to": "bob@example.com"}

FINAL: Obj = {
    "content": [{"type": "text", "text": "Done."}],
    "stop_reason": "end_turn",
    "usage": tokens(200, 3),
}
TAIL: list[JsonValue] = [
    {"type": "model_request", "actor_kind": "host", "data": {"attempt": 1}},
    {
        "type": "model_response",
        "actor_kind": "model",
        "data": {
            "content": FINAL["content"],
            "stop_reason": "end_turn",
            "completeness": "complete",
        },
    },
    {"type": "turn_completed", "data": {"reason": "end_turn"}},
]
NO_MODEL: Obj = {"responses": []}


def started(
    log: Log,
    tools: list[JsonValue],
    instructions: str = "You are a helpful agent.",
    policy: Obj | None = None,
    adapter: Obj = ADAPTER,
) -> Obj:
    cfg: Obj = {
        "agent_name": "demo",
        "instructions": instructions,
        "model": MODEL,
        "model_params": PARAMS,
        "adapter": adapter,
        "tools": tools,
    }
    if policy is not None:
        cfg["policy"] = policy
    return log.add(
        "thread_started",
        {**cfg, "config_hash": sha(canonical(cfg)), "sandbox_provider": "fake"},
    )


def user(log: Log, t: str) -> Obj:
    return log.add("user_input", {"source": "api", "text": t}, actor="user", principal=ALICE)


def read_turn(log: Log, question: str = "What is in README.md?") -> None:
    """One completed turn using a read_only tool (no effect events)."""
    user(log, question)
    r1 = log.model_request()
    log.model_response(
        r1,
        [
            {
                "type": "tool_use",
                "call_id": "call_1",
                "name": "read_file",
                "input": {"path": "README.md"},
            }
        ],
        "tool_use",
        tokens(120, 18),
    )
    log.tool_call(r1, "call_1", "read_file", {"path": "README.md"})
    log.add("permission_decision", {"call_id": "call_1", **ALLOW})
    log.add(
        "tool_result",
        {
            "call_id": "call_1",
            "is_error": False,
            "completeness": "complete",
            "preview": "# demo\n",
            "origin": "executed",
        },
        actor="tool",
    )
    r2 = log.model_request()
    log.model_response(
        r2,
        [{"type": "text", "text": "README.md contains one heading: demo."}],
        "end_turn",
        tokens(160, 12),
    )
    log.add("turn_completed", {"reason": "end_turn"})


README_BYTES = b"# demo\n"
# The fake sandbox's captured file tree: path, mode, size and sha256 per
# file, sorted by path. manifest_hash is its canonical hash, and restore verifies it.
MANIFEST: JsonValue = [
    {"path": "README.md", "mode": 0o644, "size": len(README_BYTES), "sha256": sha(README_BYTES)}
]
MANIFEST_HASH = sha(canonical(MANIFEST))
SNAPSHOT_SCRIPT: Obj = {"restore_sandbox_id": "sbx_child_01", "manifest": MANIFEST}


def snapshot(log: Log, expires_at: int | None, knowledge_revision: int | None = None) -> Obj:
    data: Obj = {
        "snapshot_id": "snap_01",
        "provider": "fake",
        "sandbox_id": "sbx_parent_01",
        "capture_class": "filesystem",
        "expires_at": expires_at,
        "manifest_hash": MANIFEST_HASH,
        "quiesced": {"frozen": [], "stopped": [], "excluded": []},
    }
    if knowledge_revision is not None:
        data["knowledge_revision"] = knowledge_revision
    return log.add("snapshot", data)


def effect_call(
    log: Log,
    spec: Obj,
    inp: Obj,
    t: str,
    **opts: Unpack[CallOpts],
) -> Obj:
    """user, model_request, tool_use response, tool_call, permission. Returns the tool_call."""
    call_id = opts.get("call_id", "call_1")
    usage = opts.get("usage")
    permission = opts.get("permission")
    user(log, t)
    r = log.model_request()
    log.model_response(
        r,
        [{"type": "tool_use", "call_id": call_id, "name": spec["name"], "input": inp}],
        "tool_use",
        usage or tokens(80, 25),
    )
    c = log.tool_call(r, call_id, text(spec["name"]), inp)
    log.add("permission_decision", {"call_id": call_id, **(permission or ALLOW)})
    return c


def base_simple() -> Log:
    log = Log()
    started(log, [READ_FILE])
    read_turn(log)
    return log


def negative(
    root: pathlib.Path,
    name: str,
    desc: str,
    log: Log,
    error: tuple[str, int] | tuple[str, int, bytes],
) -> None:
    """A reduce case that must fail with error = (code, seq[, raw log bytes])."""
    expected: Obj = {
        "outcome": "error",
        "error": {"code": error[0], "seq": error[1]},
        "appended": [],
    }
    extra: dict[str, JsonValue | bytes] = {"log.jsonl": error[2]} if len(error) == RAW_ARITY else {}
    write_case(root, case(name, "log", "reduce", desc), log, expected, extra=extra)


# ---------- helpers for the cases ----------
type Meta = tuple[str, str, str]  # (name, family, description)


def render_case(root: pathlib.Path, meta: Meta, log: Log) -> None:
    """A render case whose next request is the log's Render v1."""
    body, line0 = render(log.events, log.artifacts)
    write_case(
        root,
        case(meta[0], meta[1], "render", meta[2]),
        log,
        {
            "outcome": "ok",
            "state": reduce(log, NOW),
            "render": {
                "next_request_sha256": sha(body),
                "declared_prefix": {"bytes": len(line0), "sha256": sha(line0)},
            },
        },
        extra={"request.bytes": body},
    )


def reduce_case(root: pathlib.Path, meta: Meta, log: Log, projections: Obj) -> None:
    """A reduce case that also checks named projections."""
    write_case(
        root,
        case(meta[0], meta[1], "reduce", meta[2]),
        log,
        {"outcome": "ok", "state": reduce(log, NOW), "projections": projections},
    )


def reject(root: pathlib.Path, meta: Meta, log: Log, code: str = "invalid_transition") -> None:
    """A reduce case whose last event breaks a rule."""
    write_case(
        root,
        case(meta[0], meta[1], "reduce", meta[2]),
        log,
        {"outcome": "error", "error": {"code": code, "seq": log.seq}, "appended": []},
    )


def result(log: Log, call_id: str, preview: str, **more: JsonValue) -> Obj:
    data: Obj = {
        "call_id": call_id,
        "is_error": False,
        "completeness": "complete",
        "preview": preview,
        "origin": "executed",
        **more,
    }
    return log.add("tool_result", data, actor="tool")


def call(log: Log, name: str, inp: Obj, call_id: str = "call_1") -> Obj:
    """model_request, tool_use response, tool_call and allow. Returns the tool_call."""
    r = log.model_request()
    use: Obj = {"type": "tool_use", "call_id": call_id, "name": name, "input": inp}
    log.model_response(r, [use], "tool_use", tokens(80, 20))
    c = log.tool_call(r, call_id, name, inp)
    log.add("permission_decision", {"call_id": call_id, **ALLOW})
    return c


def answer(log: Log, t: str) -> None:
    r = log.model_request()
    log.model_response(r, [{"type": "text", "text": t}], "end_turn", tokens(90, 10))
    log.add("turn_completed", {"reason": "end_turn"})
