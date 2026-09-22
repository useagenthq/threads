# pyright: strict
"""Effect crash-recovery cases."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import BRANCH, DAY, NOW, aref, num, tokens
from .log import Log, reduce
from .pieces import (
    CHARGE,
    EMAIL,
    EMAIL_IN,
    FINAL,
    NO_MODEL,
    REFUND,
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
    # effect-crash-after-begin-idempotent
    log = Log()
    started(log, [CHARGE])
    inp: Obj = {"amount_cents": 2000, "customer": "c_42"}
    effect_call(log, CHARGE, inp, "Charge customer c_42 $20.", usage=tokens(90, 20))
    b = log.add("effect_begin", {"call_id": "call_1", "attempt": 1})
    now = num(b["time"]) + 60_000
    out = b"charged ch_001 amount_cents=2000"
    key = f"{BRANCH}:call_1"
    write_case(
        root,
        case(
            "effect-crash-after-begin-idempotent",
            "cancellation_resume",
            "recover",
            "Crash after effect_begin of an idempotent tool, inside its dedup window (provider "
            "clock minus skew). The dispatch is potentially sent, so recovery marks it unknown, "
            "settles it as safe_to_retry by provider dedup, and re-dispatches with the SAME "
            "derived effect key. The provider already executed it, so there is no new external "
            "execution.",
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
                    "type": "effect_resolved",
                    "actor_kind": "recovery",
                    "epoch": 2,
                    "data": {
                        "call_id": "call_1",
                        "outcome": "safe_to_retry",
                        "by": "provider_dedup",
                    },
                },
                {
                    "type": "effect_begin",
                    "epoch": 2,
                    "data": {"call_id": "call_1", "attempt": 2},
                },
                {
                    "type": "effect_commit",
                    "data": {
                        "call_id": "call_1",
                        "result_ref": aref(out, "text/plain"),
                    },
                },
                {
                    "type": "tool_result",
                    "actor_kind": "tool",
                    "data": {
                        "call_id": "call_1",
                        "is_error": False,
                        "origin": "executed",
                        "preview": out.decode(),
                    },
                },
                *TAIL,
            ],
            "sandbox": {
                "dispatches": {"charge_card": 1},
                "new_executions": {"charge_card": 0},
            },
        },
        extra={
            "model.json": {"responses": [FINAL]},
            "sandbox.json": {
                "tools": {
                    "charge_card": {
                        "output": "charged ch_999 amount_cents=2000",
                        "executed_keys": {key: out.decode()},
                    }
                }
            },
        },
    )

    # effect-crash-after-commit-before-result
    log = Log()
    started(log, [EMAIL])
    effect_call(log, EMAIL, EMAIL_IN, "Email bob that the build is green.")
    log.add("effect_begin", {"call_id": "call_1", "attempt": 1})
    out = b"sent message_id=<m-001@example.com>"
    log.add(
        "effect_commit",
        {
            "call_id": "call_1",
            "result_ref": log.art(out, "text/plain"),
            "provider_receipt": "m-001",
        },
    )
    write_case(
        root,
        case(
            "effect-crash-after-commit-before-result",
            "cancellation_resume",
            "recover",
            "Crash after effect_commit but before tool_result. Recovery materializes the "
            "tool_result from the commit's result_ref without dispatching the tool again, even "
            "though the tool is unguarded.",
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
                        "origin": "materialized_from_commit",
                        "preview": out.decode(),
                    },
                },
                *TAIL,
            ],
            "sandbox": {
                "dispatches": {"send_email": 0},
                "new_executions": {"send_email": 0},
            },
        },
        extra={
            "model.json": {"responses": [FINAL]},
            "sandbox.json": {
                "tools": {"send_email": {"output": "sent message_id=<m-002@example.com>"}}
            },
        },
    )

    # effect-crash-after-dispatch-unguarded-parks (the dispatch-before-durable-record window)
    log = Log()
    started(log, [EMAIL])
    effect_call(log, EMAIL, EMAIL_IN, "Email bob that the build is green.")
    log.add("effect_begin", {"call_id": "call_1", "attempt": 1})
    key = f"{BRANCH}:call_1"
    write_case(
        root,
        case(
            "effect-crash-after-dispatch-unguarded-parks",
            "cancellation_resume",
            "recover",
            "effect_begin was acked BEFORE dispatch; the process then dispatched the email and "
            "crashed before recording anything else (the provider did execute it). The effect "
            "is potentially sent and the tool is unguarded, so recovery records it unknown and "
            "parks it for a human. It never re-runs it silently and never calls the model.",
            model_script="model.json",
            sandbox_script="sandbox.json",
        ),
        log,
        {
            "outcome": "ok",
            "state": reduce(log, NOW),
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
                        "address": {"kind": "effect", "id": key},
                        "reason": "effect_unknown",
                        "expires_at": NOW + DAY,
                    },
                },
            ],
            "sandbox": {
                "dispatches": {"send_email": 0},
                "new_executions": {"send_email": 0},
            },
        },
        extra={
            "model.json": NO_MODEL,
            "sandbox.json": {
                "tools": {
                    "send_email": {
                        "output": "sent message_id=<m-002@example.com>",
                        "executed_keys": {key: "sent message_id=<m-001@example.com>"},
                    }
                }
            },
        },
    )

    # effect-reconcile-not-found-not-final-parks
    log = Log()
    started(log, [REFUND])
    effect_call(log, REFUND, {"charge": "ch_001"}, "Refund ch_001.")
    log.add("effect_begin", {"call_id": "call_1", "attempt": 1})
    key = f"{BRANCH}:call_1"
    write_case(
        root,
        case(
            "effect-reconcile-not-found-not-final-parks",
            "cancellation_resume",
            "recover",
            "Crash after effect_begin of a reconcilable tool. The adapter lookup answers "
            "not_found, but its contract does not make not_found final (eventually consistent). "
            "That is still unknown: recovery parks and never re-sends.",
            model_script="model.json",
            sandbox_script="sandbox.json",
        ),
        log,
        {
            "outcome": "ok",
            "state": reduce(log, NOW),
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
                        "address": {"kind": "effect", "id": key},
                        "reason": "effect_unknown",
                    },
                },
            ],
            "sandbox": {
                "dispatches": {"refund_card": 0},
                "new_executions": {"refund_card": 0},
                "lookups": {"refund_card": 1},
            },
        },
        extra={
            "model.json": NO_MODEL,
            "sandbox.json": {
                "tools": {
                    "refund_card": {
                        "output": "refunded",
                        "lookup": {key: {"result": "not_found", "final": False}},
                    }
                }
            },
        },
    )
