# pyright: strict
"""Recovery re-checks: timeouts, approvals, cancellation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, BRANCH, DAY, NOW, T0, sha, tool
from .jcs import canonical
from .log import Log, reduce
from .pieces import (
    EMAIL,
    EMAIL_IN,
    FINAL,
    NO_MODEL,
    SHELL,
    TAIL,
    case,
    effect_call,
    started,
    write_case,
)

if TYPE_CHECKING:
    import pathlib

    from .jcs import Obj


def build(root: pathlib.Path) -> None:
    # sandbox-local-timeout-interrupted
    log = Log()
    started(log, [SHELL])
    effect_call(log, SHELL, {"command": "make migrate"}, "Run the migration.")
    log.add("effect_begin", {"call_id": "call_1", "attempt": 1})
    log.add("effect_unknown", {"call_id": "call_1", "reason": "timeout"})
    msg = "interrupted: timed out; the command may have partly run"
    write_case(
        root,
        case(
            "sandbox-local-timeout-interrupted",
            "sandboxes",
            "recover",
            "A sandbox_local command timed out after dispatch. A timeout is uncertainty, not a "
            "plain error: it was recorded as effect_unknown. Recovery settles it only after the "
            "sandbox confirms the process group is terminated, then tells the model it was "
            "interrupted. It is never re-run automatically.",
            model_script="model.json",
            sandbox_script="sandbox.json",
        ),
        log,
        {
            "outcome": "ok",
            "state": reduce(log, NOW),
            "appended": [
                {
                    "type": "effect_resolved",
                    "actor_kind": "recovery",
                    "epoch": 2,
                    "data": {
                        "call_id": "call_1",
                        "outcome": "interrupted",
                        "by": "sandbox_terminated",
                    },
                },
                {
                    "type": "tool_result",
                    "actor_kind": "recovery",
                    "data": {
                        "call_id": "call_1",
                        "is_error": True,
                        "origin": "interrupted",
                        "preview": msg,
                    },
                },
                *TAIL,
            ],
            "sandbox": {
                "dispatches": {"run_shell": 0},
                "new_executions": {"run_shell": 0},
            },
        },
        extra={
            "model.json": {"responses": [FINAL]},
            "sandbox.json": {"tools": {"run_shell": {"output": "ok", "process": "terminated"}}},
        },
    )

    # recovery-approval-pending-parks
    log = Log()
    started(log, [EMAIL])
    effect_call(
        log,
        EMAIL,
        EMAIL_IN,
        "Email bob that the build is green.",
        permission={
            "decision": "ask",
            "source": "policy",
            "rule_id": "email_needs_approval",
        },
    )
    ch = "0192c000-0000-7000-8000-000000000001"
    log.add(
        "approval_requested",
        {
            "challenge_id": ch,
            "call_id": "call_1",
            "args_hash": sha(canonical(EMAIL_IN)),
            "expires_at": T0 + 3_600_000,
        },
    )
    write_case(
        root,
        case(
            "recovery-approval-pending-parks",
            "permissions_approvals",
            "recover",
            "Crash while a call awaits approval. The call has no effect_begin, but 'not "
            "started' is not permission: recovery re-reads the approval state, finds it "
            "pending, and parks. Nothing is dispatched.",
            model_script="model.json",
            sandbox_script="sandbox.json",
        ),
        log,
        {
            "outcome": "ok",
            "state": reduce(log, NOW),
            "appended": [
                {
                    "type": "parked",
                    "actor_kind": "recovery",
                    "epoch": 2,
                    "data": {
                        "address": {"kind": "approval", "id": ch},
                        "reason": "awaiting_approval",
                    },
                }
            ],
            "sandbox": {"dispatches": {"send_email": 0}},
        },
        extra={
            "model.json": NO_MODEL,
            "sandbox.json": {"tools": {"send_email": {"output": "sent"}}},
        },
    )

    # recovery-cancelled-call-not-dispatched
    log = Log()
    started(log, [EMAIL])
    effect_call(log, EMAIL, EMAIL_IN, "Email bob that the build is green.")
    cr = log.add("cancel_requested", {"scope": "turn"}, actor="user", principal=ALICE)
    write_case(
        root,
        case(
            "recovery-cancelled-call-not-dispatched",
            "cancellation_resume",
            "recover",
            "Crash after an allowed call was recorded and the turn was then cancelled. Recovery "
            "honours the durable cancellation barrier: the call is closed as not_executed, "
            "never dispatched, and the turn ends cancelled.",
            model_script="model.json",
            sandbox_script="sandbox.json",
        ),
        log,
        {
            "outcome": "ok",
            "state": reduce(log, NOW),
            "appended": [
                {
                    "type": "tool_result",
                    "actor_kind": "recovery",
                    "epoch": 2,
                    "data": {
                        "call_id": "call_1",
                        "is_error": True,
                        "origin": "not_executed",
                        "preview": "not executed: cancelled",
                    },
                },
                {"type": "cancelled", "data": {"request_event_id": cr["event_id"]}},
                {"type": "turn_completed", "data": {"reason": "cancelled"}},
            ],
            "sandbox": {"dispatches": {"send_email": 0}},
        },
        extra={
            "model.json": NO_MODEL,
            "sandbox.json": {"tools": {"send_email": {"output": "sent"}}},
        },
    )
    _confirmed_before_result(root)
    _dedup_window_from_first_attempt(root)


def _confirmed_before_result(root: pathlib.Path) -> None:
    log = Log()
    started(log, [EMAIL])
    effect_call(log, EMAIL, EMAIL_IN, "Email bob that the build is green.")
    log.add("effect_begin", {"call_id": "call_1", "attempt": 1})
    log.add("effect_unknown", {"call_id": "call_1", "reason": "crash_after_begin"})
    out = b"sent message_id=<m-001@example.com>"
    resolved: Obj = {
        "call_id": "call_1",
        "outcome": "confirmed_success",
        "by": "reconcile",
        "result_ref": log.art(out, "text/plain"),
    }
    log.add("effect_resolved", resolved)
    write_case(
        root,
        case(
            "effect-confirmed-before-result",
            "cancellation_resume",
            "recover",
            "Crash after effect_resolved{confirmed_success} but before its tool_result. The "
            "settlement is final: recovery appends the result from the resolution's result_ref "
            "and never dispatches the tool again, whatever its class. Only safe_to_retry, "
            "not_sent and assume_not_done may lead to another dispatch.",
            model_script="model.json",
            sandbox_script="sandbox.json",
        ),
        log,
        {
            "outcome": "ok",
            "state": reduce(log, NOW),
            "appended": [
                {
                    "type": "tool_result",
                    "actor_kind": "recovery",
                    "epoch": 2,
                    "data": {
                        "call_id": "call_1",
                        "is_error": False,
                        "origin": "executed",
                        "preview": out.decode(),
                    },
                },
                *TAIL,
            ],
            "sandbox": {"dispatches": {"send_email": 0}, "new_executions": {"send_email": 0}},
        },
        extra={
            "model.json": {"responses": [FINAL]},
            "sandbox.json": {"tools": {"send_email": {"output": "sent message_id=<m-002>"}}},
        },
    )


def _dedup_window_from_first_attempt(root: pathlib.Path) -> None:
    # A 10 s dedup window. The first send (seq 7, T0 + 7 s) was deduped and re-sent at seq 10
    # (T0 + 10 s), which crashed too. At T0 + 18 s the key is 11 s past its FIRST acceptance:
    # outside the window, even though the last send is only 8 s old.
    charge = tool(
        "charge_card",
        "Charge a customer card.",
        {"amount_cents": {"type": "integer"}, "customer": {"type": "string"}},
        "idempotent",
        10_000,
    )
    log = Log()
    started(log, [charge])
    inp: Obj = {"amount_cents": 2000, "customer": "c_42"}
    effect_call(log, charge, inp, "Charge customer c_42 $20.")
    log.add("effect_begin", {"call_id": "call_1", "attempt": 1})
    log.add("effect_unknown", {"call_id": "call_1", "reason": "crash_after_begin"})
    retry: Obj = {"call_id": "call_1", "outcome": "safe_to_retry", "by": "provider_dedup"}
    log.add("effect_resolved", retry)
    log.add("effect_begin", {"call_id": "call_1", "attempt": 2})
    now = T0 + 18_000
    write_case(
        root,
        case(
            "effect-dedup-window-from-first-attempt",
            "cancellation_resume",
            "recover",
            "An idempotent effect crashed twice: the first attempt was settled safe_to_retry by "
            "provider dedup and re-sent, and the re-send crashed too. A deduped re-send doesn't "
            "renew the provider's retention, so the window runs from the FIRST attempt under the "
            "key. It has passed, so recovery marks the effect unknown and parks it, although the "
            "last attempt alone would still be inside the window.",
            now,
            model_script="model.json",
            sandbox_script="sandbox.json",
        ),
        log,
        {
            "outcome": "ok",
            "state": reduce(log, now),
            "appended": [
                {
                    "type": "effect_unknown",
                    "actor_kind": "recovery",
                    "epoch": 2,
                    "data": {"call_id": "call_1", "reason": "crash_after_begin"},
                },
                {
                    "type": "parked",
                    "actor_kind": "recovery",
                    "epoch": 2,
                    "data": {
                        "address": {"kind": "effect", "id": f"{BRANCH}:call_1"},
                        "reason": "effect_unknown",
                        "expires_at": now + DAY,
                    },
                },
            ],
            "sandbox": {"dispatches": {"charge_card": 0}, "new_executions": {"charge_card": 0}},
        },
        extra={
            "model.json": NO_MODEL,
            "sandbox.json": {
                "tools": {"charge_card": {"output": "charged ch_999 amount_cents=2000"}}
            },
        },
    )
