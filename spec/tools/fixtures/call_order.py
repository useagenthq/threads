# pyright: strict
"""A response with several tool calls (spec/schema/README.md, "Recording a response's calls"):
each call is recorded and authorized before the next part is recorded, and none runs before
every part is. Both runtimes append exactly these events, in this order."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .common import ALICE, ALLOW, NOW, sha, tokens
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


@dataclass(frozen=True, slots=True)
class Run:
    """What the resumed run appends, the model answers it gets, and the scripted tools."""

    appended: list[JsonValue]
    responses: list[JsonValue]
    sandbox: Obj
    dispatches: Obj | None = None
    """Per-tool dispatch counts; default: read_file, once per executed result."""


def _write(root: pathlib.Path, name: str, description: str, log: Log, run: Run) -> None:
    reads = sum(1 for e in run.appended if isinstance(e, dict) and e.get("actor_kind") == "tool")
    dispatches = run.dispatches or {"read_file": reads}
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
            "appended": run.appended,
            "sandbox": {"dispatches": dispatches},
        },
        extra={"model.json": {"responses": run.responses}, "sandbox.json": run.sandbox},
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
        Run(
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
        Run(
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
        ),
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
        Run(
            [
                *_requested(uses),
                *recorded,
                *(_read_result(f"call_{i}") for i in (1, 2, 3)),
                *TAIL,
            ],
            [_response(uses), FINAL],
            {"tools": {"read_file": {"output": OUT, "concurrent": True}}},
        ),
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
        Run(
            [*_recorded(b), _read_result("call_1"), _read_result("call_2"), *TAIL],
            [FINAL],
            _tools(),
        ),
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
        Run(
            [
                _decided("call_1"),
                _decided("call_2"),
                _read_result("call_1"),
                _read_result("call_2"),
                *TAIL,
            ],
            [FINAL],
            _tools(),
        ),
    )


def _responded(root: pathlib.Path) -> None:
    """Crash right after the response: none of its calls is recorded yet."""
    a, b = _read("call_1", "README.md"), _read("call_2", "NOTES.md")
    log = _fresh()
    r = log.model_request()
    log.model_response(r, [a, b], "tool_use", tokens(80, 25))
    _write(
        root,
        "calls-recording-resumes-after-response",
        "Crash right after a model_response with two calls, before either was recorded. "
        "Nothing is pending, but recovery doesn't close the turn interrupted: the response "
        "still owes its calls, so the run records and authorizes both, runs them and "
        "continues as an uninterrupted run would.",
        log,
        Run(
            [*_recorded(a), *_recorded(b), _read_result("call_1"), _read_result("call_2"), *TAIL],
            [FINAL],
            _tools(),
        ),
    )


def _refused(root: pathlib.Path) -> None:
    """Crash after the first call was refused as invalid, before the second was recorded."""
    bad: Obj = {"to": "bob@example.com"}
    a, b = _use("call_1", "send_email", bad), _read("call_2", "README.md")
    log = _fresh()
    r = log.model_request()
    log.model_response(r, [a, b], "tool_use", tokens(80, 25))
    log.tool_call(r, "call_1", "send_email", bad)
    refused: Obj = {
        "call_id": "call_1",
        "completeness": "complete",
        "is_error": True,
        "origin": "not_executed",
        "preview": "invalid input: body is required",
    }
    log.add("tool_result", refused)
    _write(
        root,
        "calls-recording-resumes-after-refused-call",
        "Crash after the first call, send_email without a body, was recorded with its "
        "not_executed result, before the second part was recorded. Nothing is pending, but the "
        "response still owes a call: the run records and runs the read, and send_email never "
        "runs.",
        log,
        Run(
            [*_recorded(b), _read_result("call_2"), *TAIL],
            [FINAL],
            _tools(send_email={"output": "sent"}),
            {"read_file": 1, "send_email": 0},
        ),
    )


def _cancelled(root: pathlib.Path) -> None:
    """A cancel landed after the first of three calls was recorded and authorized."""
    a, b, c = (_read(f"call_{i}", f"doc{i}.md") for i in (1, 2, 3))
    log = _fresh()
    r = log.model_request()
    log.model_response(r, [a, b, c], "tool_use", tokens(80, 25))
    log.tool_call(r, "call_1", "read_file", {"path": "doc1.md"})
    log.add("permission_decision", {"call_id": "call_1", **ALLOW, "mode": "default"})
    cancel = log.add("cancel_requested", {"scope": "turn"}, actor="user", principal=ALICE)

    def closed(call_id: str, actor: str) -> Obj:
        data: Obj = {
            "call_id": call_id,
            "is_error": True,
            "origin": "not_executed",
            "preview": "not executed: cancelled",
        }
        return {"type": "tool_result", "actor_kind": actor, "data": data}

    _write(
        root,
        "calls-cancel-mid-recording-closes-the-rest",
        "A cancel_requested landed after the first of three calls was recorded and authorized. "
        "Nothing new is authorized after it: recovery closes the recorded call, and the "
        "cancellation step records each remaining part with its not_executed result, so no "
        "tool_use is left without a result, then ends the turn cancelled.",
        log,
        Run(
            [
                closed("call_1", "recovery"),
                _call(b),
                closed("call_2", "host"),
                _call(c),
                closed("call_3", "host"),
                {"type": "cancelled", "data": {"request_event_id": cancel["event_id"]}},
                {"type": "turn_completed", "data": {"reason": "cancelled"}},
            ],
            [],
            _tools(),
        ),
    )


def build(root: pathlib.Path) -> None:
    _mixed(root)
    _park_in_middle(root)
    _group(root)
    _resumed(root)
    _lazy(root)
    _responded(root)
    _refused(root)
    _cancelled(root)
