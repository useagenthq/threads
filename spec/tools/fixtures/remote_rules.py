# pyright: strict
"""One negative case per A2A semantic rule (spec/schema/README.md, rules 56-58).

A remote called as a tool is an ordinary tool call, so each log here is a real `reconcilable` call
(REFUND stands in for the send tool) whose dispatch is an A2A exchange. That matters: the call_id a
`remote_call` names is the tool call's, which is what lets the existing effect rules see it.

The three cases are the shape of a real mistake rather than a synthetic one:

* 56 — a `remote_call` written without the `effect_begin` that belongs to it. A begin that is not in
  the same append is a call that could be dispatched before anything durable said so.
* 57 — a partner task state recorded for a call whose effect never committed: claiming to have
  watched a task we hold no receipt for.
* 58 — a second `remote_call` under one `call_id`, which is how a resend would come to send freshly
  serialized bytes instead of the ones it stored, and so a different `messageId` to a peer that
  deduplicates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import aref, num
from .log import Log
from .pieces import REFUND, effect_call, negative, started

if TYPE_CHECKING:
    import pathlib

CARD = b'{"name":"refunds"}'
REQUEST = b'{"message":{"messageId":"m","role":"ROLE_USER","parts":[{"text":"hi"}]}}'

_CALL = "call_1"
_CONTEXT = "0192f4c1-0000-8000-8000-000000000002"
_MESSAGE = "0192f4c1-0000-8000-8000-000000000003"
_TASK = "task-1"


def _opened() -> Log:
    """A thread that pinned a partner's card and then called the remote's send tool."""
    log = Log()
    started(log, [REFUND])
    log.add(
        "remote_card",
        {
            "remote": "refunds",
            "card_ref": aref(CARD, "application/json"),
            "interface_url": "https://refunds.partner.example/a2a/v1",
            "binding": "JSONRPC",
        },
    )
    effect_call(log, REFUND, {"charge": "ch_1042"}, "refund order 1042")
    return log


def _call(log: Log) -> None:
    log.add(
        "remote_call",
        {
            "call_id": _CALL,
            "remote": "refunds",
            "operation": "send_message",
            "message_id": _MESSAGE,
            "context_id": _CONTEXT,
            "request_ref": aref(REQUEST, "application/json"),
        },
    )


def build(root: pathlib.Path) -> None:
    # Rule 56: the begin belongs to the same append, so anything else next is refused.
    log = _opened()
    _call(log)
    e = log.add("turn_completed", {"reason": "end_turn"})
    negative(
        root,
        "remote-call-without-effect-begin-rejected",
        "A remote_call not directly followed by the effect_begin of its call: invalid_transition.",
        log,
        ("invalid_transition", num(e["seq"])),
    )

    # Rule 57: a partner's state is only ever observed for a call we hold a receipt for.
    log = _opened()
    _call(log)
    log.add("effect_begin", {"call_id": _CALL, "attempt": 1})
    e = log.add(
        "remote_task_state",
        {"call_id": _CALL, "task_id": _TASK, "state": "TASK_STATE_WORKING"},
        # An observation, not a decision: a reader that does not know it can skip it.
        critical=False,
    )
    negative(
        root,
        "remote-task-state-without-commit-rejected",
        "A remote_task_state for a call whose effect has no commit: invalid_transition.",
        log,
        ("invalid_transition", num(e["seq"])),
    )

    # Rule 58: one stored request body per call, so a resend cannot write a second one.
    log = _opened()
    _call(log)
    log.add("effect_begin", {"call_id": _CALL, "attempt": 1})
    log.add(
        "effect_resolved",
        {"call_id": _CALL, "outcome": "safe_to_retry", "by": "provider_dedup"},
    )
    e = log.add(
        "remote_call",
        {
            "call_id": _CALL,
            "remote": "refunds",
            "operation": "send_message",
            "message_id": _MESSAGE,
            "context_id": _CONTEXT,
            "request_ref": aref(REQUEST, "application/json"),
        },
    )
    negative(
        root,
        "remote-call-twice-rejected",
        "A second remote_call under one call_id: invalid_transition.",
        log,
        ("invalid_transition", num(e["seq"])),
    )
