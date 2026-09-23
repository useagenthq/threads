# pyright: strict
"""Hook cases: a before_tool deny and a before_tool that fails
closed, an after_tool failure that neither undoes nor repeats a committed effect, and the
bounded on_stop continuation. The logs are what a runtime records; the runtimes' own tests run
the hooks that produce them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import NOW, tokens
from .log import Log, reduce
from .pieces import (
    EMAIL,
    EMAIL_IN,
    FINAL,
    answer,
    case,
    render_case,
    result,
    started,
    user,
    write_case,
)

if TYPE_CHECKING:
    import pathlib

    from .jcs import Obj

FAM = "hooks"
STOP_LIMIT = 3  # at most three on_stop continuations per turn
FINAL_CONTENT = list(FINAL["content"]) if isinstance(FINAL["content"], list) else []


def build(root: pathlib.Path) -> None:
    _deny(root)
    _fails_closed(root)
    _after_tool(root)
    _stop(root)


def _hook(log: Log, hook: str, decision: str, **more: str) -> Obj:
    return log.add(
        "hook_decision", {"extension": "guard", "hook": hook, "decision": decision, **more}
    )


def _denied_call(log: Log, decision: str, reason: str) -> None:
    """The call is recorded, the hook's decision folds into a hook-sourced deny, and the model
    gets the reason as a denied result. No effect event exists."""
    user(log, "Email bob the build status.")
    r = log.model_request()
    use: Obj = {"type": "tool_use", "call_id": "call_1", "name": "send_email", "input": EMAIL_IN}
    log.model_response(r, [use], "tool_use", tokens(80, 25))
    log.tool_call(r, "call_1", "send_email", EMAIL_IN)
    _hook(log, "before_tool", decision, reason=reason, call_id="call_1")
    log.add(
        "permission_decision",
        {"call_id": "call_1", "decision": "deny", "source": "hook", "reason": reason},
    )
    log.add(
        "tool_result",
        {
            "call_id": "call_1",
            "is_error": True,
            "completeness": "complete",
            "origin": "denied",
            "preview": f"denied: {reason}",
        },
    )


def _deny(root: pathlib.Path) -> None:
    log = Log()
    started(log, [EMAIL])
    _denied_call(log, "deny", "outbound email is off")
    render_case(
        root,
        (
            "hook-before-tool-deny",
            FAM,
            "A before_tool hook denies the call. Its hook_decision is recorded before the "
            "permission_decision, which carries source hook and the reason; the tool never "
            "runs (no effect event), and the next request shows the model the reason.",
        ),
        log,
    )


def _fails_closed(root: pathlib.Path) -> None:
    log = Log()
    started(log, [EMAIL])
    _denied_call(log, "failed", "timed out after 5000 ms")
    answer(log, "I couldn't send it: the send was blocked.")
    write_case(
        root,
        case(
            "hook-before-tool-fails-closed",
            FAM,
            "reduce",
            "A before_tool hook times out. A gating hook's failure is recorded as failed and "
            "denies the call (source hook), so nothing is dispatched; the run continues and "
            "the turn completes.",
        ),
        log,
        {"outcome": "ok", "state": reduce(log, NOW)},
    )


def _after_tool(root: pathlib.Path) -> None:
    log = Log()
    started(log, [EMAIL])
    user(log, "Email bob the build status.")
    r = log.model_request()
    use: Obj = {"type": "tool_use", "call_id": "call_1", "name": "send_email", "input": EMAIL_IN}
    log.model_response(r, [use], "tool_use", tokens(80, 25))
    log.tool_call(r, "call_1", "send_email", EMAIL_IN)
    log.add(
        "permission_decision",
        {
            "call_id": "call_1",
            "decision": "allow",
            "source": "policy",
            "rule_id": "conformance_allow",
        },
    )
    log.add("effect_begin", {"call_id": "call_1", "attempt": 1})
    out = b"sent message_id=<m-001@example.com>"
    log.add("effect_commit", {"call_id": "call_1", "result_ref": log.art(out, "text/plain")})
    result(log, "call_1", out.decode())
    _hook(log, "after_tool", "failed", reason="threw: audit sink down", call_id="call_1")
    answer(log, "Sent.")
    write_case(
        root,
        case(
            "hook-after-tool-failure-no-reexec",
            FAM,
            "reduce",
            "An after_tool hook throws after the effect committed. The failure is recorded "
            "after the result; the effect stays committed, is not denied after the fact and is "
            "not executed again (one effect_begin), and the turn completes.",
        ),
        log,
        {"outcome": "ok", "state": reduce(log, NOW)},
    )


def _stop(root: pathlib.Path) -> None:
    log = Log()
    started(log, [])
    user(log, "Fix the failing test.")
    reason = "The tests still fail: run them again."
    for i in range(STOP_LIMIT + 1):
        r = log.model_request()
        log.model_response(r, FINAL_CONTENT, "end_turn", tokens(200 + i, 3))
        req = str(r["event_id"])
        _hook(log, "on_stop", "continue", reason=reason, request_event_id=req)
        if i < STOP_LIMIT:
            log.add(
                "injected",
                {
                    "source": "hook",
                    "trust": "trusted_instruction",
                    "origin": {"id": "guard"},
                    "text": reason,
                },
            )
    log.add("turn_completed", {"reason": "stop_hook_limit"})
    write_case(
        root,
        case(
            "hook-stop-continue-bounded",
            FAM,
            "reduce",
            "An on_stop hook answers continue every time. Each continue is recorded with its "
            "reason and forces one more request through a trusted hook instruction, three "
            "times; the fourth continue ends the turn stop_hook_limit.",
        ),
        log,
        {"outcome": "ok", "state": reduce(log, NOW)},
    )
