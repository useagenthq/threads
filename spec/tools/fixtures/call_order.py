# pyright: strict
"""A response with several tool calls (spec/schema/README.md, "Recording a response's calls"):
each call is recorded and authorized before the next part is recorded, and none runs before
every part is. Both runtimes append exactly these events, in this order."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALLOW, NOW, sha, tokens
from .jcs import canonical
from .log import Log, reduce
from .pieces import CHARGE, EMAIL, EMAIL_IN, FINAL, READ_FILE, TAIL, case, started, user, write_case

if TYPE_CHECKING:
    import pathlib

    from .jcs import JsonValue, Obj

FAM = "permissions_approvals"
HOUR = 3_600_000
OUT = "# demo\n"
CHARGE_IN: Obj = {"amount_cents": 2000, "customer": "c_42"}


def _use(call_id: str, name: str, inp: Obj) -> Obj:
    return {"type": "tool_use", "call_id": call_id, "name": name, "input": inp}


def _read(call_id: str, path: str) -> Obj:
    return _use(call_id, "read_file", {"path": path})


def _response(uses: list[JsonValue]) -> Obj:
    return {"content": uses, "stop_reason": "tool_use", "usage": tokens(80, 25)}


def _requested(uses: list[JsonValue]) -> list[JsonValue]:
    return [
        {"type": "model_request", "actor_kind": "host", "data": {"attempt": 1}},
        {
            "type": "model_response",
            "actor_kind": "model",
            "data": {"content": uses, "stop_reason": "tool_use", "completeness": "complete"},
        },
    ]


def _call(use: Obj) -> Obj:
    data = {k: use[k] for k in ("call_id", "name", "input")}
    return {"type": "tool_call", "actor_kind": "host", "data": data}


def _decided(call_id: str, decision: str = "allow") -> Obj:
    data: Obj = {
        "call_id": call_id,
        "decision": decision,
        "source": "policy",
        "rule_id": f"conformance_{decision}",
        "mode": "default",
    }
    return {"type": "permission_decision", "actor_kind": "host", "data": data}


def _recorded(use: Obj, decision: str = "allow") -> list[JsonValue]:
    return [_call(use), _decided(str(use["call_id"]), decision)]


def _read_result(call_id: str) -> Obj:
    data: Obj = {"call_id": call_id, "is_error": False, "origin": "executed", "preview": OUT}
    return {"type": "tool_result", "actor_kind": "tool", "data": data}


def _asked(call_id: str, inp: Obj) -> list[JsonValue]:
    """An ask's turn to run: its challenge opens, then the run parks on it."""
    challenge: Obj = {
        "call_id": call_id,
        "args_hash": sha(canonical(inp)),
        "expires_at": NOW + HOUR,
    }
    park: Obj = {
        "address": {"kind": "approval"},
        "reason": "awaiting_approval",
        "expires_at": NOW + HOUR,
    }
    return [
        {"type": "approval_requested", "actor_kind": "host", "data": challenge},
        {"type": "parked", "actor_kind": "host", "data": park},
    ]


def _fresh() -> Log:
    """An open turn whose input was never sent: the run continues it."""
    log = Log()
    started(log, [READ_FILE, EMAIL, CHARGE])
    user(log, "Read the files, email Bob and charge c_42.")
    return log


def _write(
    root: pathlib.Path,
    name: str,
    description: str,
    log: Log,
    appended: list[JsonValue],
    responses: list[JsonValue],
    sandbox: Obj,
) -> None:
    reads = sum(1 for e in appended if isinstance(e, dict) and e.get("actor_kind") == "tool")
    write_case(
        root,
        case(
            name,
            FAM,
            "recover",
            description,
            NOW,
            model_script="model.json",
            sandbox_script="sandbox.json",
        ),
        log,
        {
            "outcome": "ok",
            "state": reduce(log, NOW),
            "appended": appended,
            "sandbox": {"dispatches": {"read_file": reads}},
        },
        extra={"model.json": {"responses": responses}, "sandbox.json": sandbox},
    )


def _tools(**changes: Obj) -> Obj:
    tools: Obj = {"read_file": {"output": OUT}, **changes}
    return {"tools": tools}


def _mixed(root: pathlib.Path) -> None:
    a, b = _read("call_1", "README.md"), _use("call_2", "send_email", EMAIL_IN)
    ask = _use("call_3", "charge_card", CHARGE_IN)
    uses: list[JsonValue] = [a, b, ask]
    denied: Obj = {"call_id": "call_2", "is_error": True, "origin": "denied", "preview": "denied"}
    _write(
        root,
        "calls-mixed-decisions-recorded-in-part-order",
        "One response calls read_file (allowed), send_email (denied) and charge_card (ask). Each "
        "tool_call is followed by its permission_decision before the next part is recorded; "
        "then, in call order, the read runs, the deny closes with `denied`, and the ask opens "
        "its challenge and parks the run.",
        _fresh(),
        [
            *_requested(uses),
            *_recorded(a),
            *_recorded(b, "deny"),
            *_recorded(ask, "ask"),
            _read_result("call_1"),
            {"type": "tool_result", "actor_kind": "host", "data": denied},
            *_asked("call_3", CHARGE_IN),
        ],
        [_response(uses)],
        _tools(
            send_email={"output": "sent", "decision": "deny"},
            charge_card={"output": "charged", "decision": "ask"},
        ),
    )


def _park_in_middle(root: pathlib.Path) -> None:
    a, b = _read("call_1", "README.md"), _use("call_2", "charge_card", CHARGE_IN)
    c = _read("call_3", "NOTES.md")
    uses: list[JsonValue] = [a, b, c]
    _write(
        root,
        "calls-ask-parks-between-reads",
        "One response calls read_file, charge_card (ask) and read_file again. All three are "
        "recorded and authorized first; the first read runs, then the ask parks the run, so "
        "the third call stays pending, authorized but not run.",
        _fresh(),
        [
            *_requested(uses),
            *_recorded(a),
            *_recorded(b, "ask"),
            *_recorded(c),
            _read_result("call_1"),
            *_asked("call_2", CHARGE_IN),
        ],
        [_response(uses)],
        _tools(charge_card={"output": "charged", "decision": "ask"}),
    )


def _group(root: pathlib.Path) -> None:
    uses: list[JsonValue] = [_read(f"call_{i}", f"doc{i}.md") for i in (1, 2, 3)]
    recorded = [e for u in uses if isinstance(u, dict) for e in _recorded(u)]
    _write(
        root,
        "calls-concurrent-reads-recorded-like-sequential",
        "One response calls a concurrent read_only tool three times. The three calls are "
        "recorded and authorized in part order before the group starts; the reads run together "
        "and their results are recorded in call order, so the log equals a sequential run's.",
        _fresh(),
        [
            *_requested(uses),
            *recorded,
            *(_read_result(f"call_{i}") for i in (1, 2, 3)),
            *TAIL,
        ],
        [_response(uses), FINAL],
        {"tools": {"read_file": {"output": OUT, "concurrent": True}}},
    )


def _resumed(root: pathlib.Path) -> None:
    """Crash after the first call was recorded and authorized, before the second was recorded."""
    a, b = _read("call_1", "README.md"), _read("call_2", "NOTES.md")
    log = _fresh()
    r = log.model_request()
    log.model_response(r, [a, b], "tool_use", tokens(80, 25))
    log.tool_call(r, "call_1", "read_file", {"path": "README.md"})
    log.add("permission_decision", {"call_id": "call_1", **ALLOW, "mode": "default"})
    _write(
        root,
        "calls-recording-resumes-mid-response",
        "Crash after the first of two calls was recorded and authorized. The resumed run "
        "records and authorizes the second before either runs, so the log continues as an "
        "uninterrupted run's would.",
        log,
        [*_recorded(b), _read_result("call_1"), _read_result("call_2"), *TAIL],
        [FINAL],
        _tools(),
    )


def _lazy(root: pathlib.Path) -> None:
    """A log from before calls were authorized as they were recorded: both calls, no decision."""
    log = _fresh()
    r = log.model_request()
    log.model_response(
        r, [_read("call_1", "README.md"), _read("call_2", "NOTES.md")], "tool_use", tokens(80, 25)
    )
    log.tool_call(r, "call_1", "read_file", {"path": "README.md"})
    log.tool_call(r, "call_2", "read_file", {"path": "NOTES.md"})
    _write(
        root,
        "calls-recorded-then-authorized-resumes",
        "A log written when calls were all recorded before any was authorized (Python before "
        "0.1) reads and reduces. Resumed, its undecided calls are authorized in call order "
        "before either runs.",
        log,
        [
            _decided("call_1"),
            _decided("call_2"),
            _read_result("call_1"),
            _read_result("call_2"),
            *TAIL,
        ],
        [FINAL],
        _tools(),
    )


def build(root: pathlib.Path) -> None:
    _mixed(root)
    _park_in_middle(root)
    _group(root)
    _resumed(root)
    _lazy(root)
