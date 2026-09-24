# pyright: strict
"""Pieces of the `ui` cases: runs of the shapes the cases stream, and the case writer that runs
the reference connection over them (spec/conformance/README.md, "ui")."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .agents import ASK, SPAWN
from .common import ALICE, ALLOW, NOW, T0, obj, sha, text, tokens
from .jcs import canonical
from .log import Log
from .pieces import READ_FILE, SHELL, case, started, write_case
from .ui_run import run_case

if TYPE_CHECKING:
    import pathlib

    from .jcs import JsonValue, Obj

FAM = "tools_streaming"
PROTOCOLS = ("ai-sdk", "ag-ui")
TOOLS: list[JsonValue] = [READ_FILE, SHELL, ASK, SPAWN]
CHILD = "0192a000-0000-7000-8000-0000000000d1"
LATER = T0 + 3_600_000
EXPIRED = T0 + 30_000
ASK_: Obj = {"decision": "ask", "source": "policy"}
CLIENT_IDS: Obj = {"threadId": "chat-1", "runId": "run-a"}


def ui_log() -> Log:
    log = Log()
    started(log, TOOLS)
    return log


def user(log: Log, t: str, client_id: str | None = None) -> Obj:
    data: Obj = {"source": "api", "text": t}
    if client_id is not None:
        data["client_message_id"] = client_id
    return log.add("user_input", data, actor="user", principal=ALICE)


def respond(log: Log, content: list[JsonValue], stop: str = "end_turn") -> tuple[Obj, Obj]:
    """A turn request and its response."""
    r = log.model_request()
    return r, log.model_response(r, content, stop, tokens(100, 20))


def say(t: str) -> Obj:
    return {"type": "text", "text": t}


def use(call_id: str, name: str, inp: Obj) -> Obj:
    return {"type": "tool_use", "call_id": call_id, "name": name, "input": inp}


def called(log: Log, req: Obj, part: Obj, *, allow: bool = True) -> None:
    """The tool_call of a tool_use part and its permission decision."""
    call_id = text(part["call_id"])
    log.tool_call(req, call_id, text(part["name"]), obj(part["input"]))
    log.add("permission_decision", {"call_id": call_id, **(ALLOW if allow else ASK_)})


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


def end(log: Log, reason: str = "end_turn", **more: JsonValue) -> None:
    log.add("turn_completed", {"reason": reason, **more})


def asked(log: Log, call_id: str, challenge: str, cmd: str, expires: int = LATER) -> Obj:
    """A shell call that asks for approval and parks on it; returns the binding."""
    inp: Obj = {"command": cmd}
    r, _ = respond(log, [use(call_id, "run_shell", inp)], "tool_use")
    called(log, r, use(call_id, "run_shell", inp), allow=False)
    binding: Obj = {"challenge_id": challenge, "call_id": call_id, "args_hash": sha(canonical(inp))}
    log.add("approval_requested", {**binding, "expires_at": expires})
    log.add(
        "parked", {"address": {"kind": "approval", "id": challenge}, "reason": "awaiting_approval"}
    )
    return binding


def decided(log: Log, binding: Obj, grant: bool, reason: str | None = None) -> Obj:
    data: Obj = dict(binding)
    if reason is not None:
        data["reason"] = reason
    kind = "approval_granted" if grant else "approval_denied"
    e = log.add(kind, data, actor="approver", principal=ALICE)
    address: Obj = {"kind": "approval", "id": binding["challenge_id"]}
    log.add("resumed", {"address": address, "cause_event_id": e["event_id"]})
    return e


def question(log: Log, call_id: str, q: str) -> None:
    inp: Obj = {"question": q}
    r, _ = respond(log, [use(call_id, "ask_user", inp)], "tool_use")
    called(log, r, use(call_id, "ask_user", inp))
    address: Obj = {"kind": "input", "id": call_id}
    log.add(
        "parked", {"address": address, "reason": "awaiting_input", "expires_at": T0 + 86_400_000}
    )


def answered(log: Log, call_id: str, text: str) -> Obj:
    data: Obj = {
        "call_id": call_id,
        "is_error": False,
        "completeness": "complete",
        "preview": text,
        "origin": "answered",
    }
    e = log.add("tool_result", data, actor="user", principal=ALICE)
    address: Obj = {"kind": "input", "id": call_id}
    log.add("resumed", {"address": address, "cause_event_id": e["event_id"]})
    return e


def ui_case(
    root: pathlib.Path,
    name: str,
    desc: str,
    log: Log,
    inp: Obj,
) -> list[bytes]:
    """Writes one `ui` case: the log, the frames the reference connection sends, the messages."""
    lines, messages = run_case(log.events, inp, NOW)
    listed: list[JsonValue] = [*messages]
    write_case(
        root,
        case(name, FAM, "ui", desc, NOW, input=inp),
        log,
        {"outcome": "ok", "messages": listed},
        extra={"frames.jsonl": b"".join(line + b"\n" for line in lines)},
    )
    return lines


def both(root: pathlib.Path, name: str, desc: str, log: Log, inp: Obj) -> None:
    """The case once per protocol, `<name>-ai-sdk` and `<name>-ag-ui`."""
    for p in PROTOCOLS:
        ui_case(root, f"{name}-{p}", desc, log, {"protocol": p, **inp})
