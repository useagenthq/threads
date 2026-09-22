# pyright: strict
"""One negative case per semantic rule."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, ALLOW, T0, num, sha, tokens
from .jcs import canonical
from .log import Log
from .pieces import (
    EMAIL,
    EMAIL_IN,
    READ_FILE,
    TESTS,
    base_simple,
    effect_call,
    negative,
    started,
    user,
)

if TYPE_CHECKING:
    import pathlib


def build(root: pathlib.Path) -> None:
    # semantic-rule negatives (spec/schema/README.md, Semantic rules)
    log = Log()
    started(log, [READ_FILE])
    log.epoch = 2
    user(log, "hello")
    log.epoch = 1
    e = log.add("turn_completed", {"reason": "end_turn"})
    negative(
        root,
        "epoch-decrease-rejected",
        "An event's epoch is lower than the previous event's: invalid_transition.",
        log,
        ("invalid_transition", num(e["seq"])),
    )

    log = Log()
    started(log, [READ_FILE])
    user(log, "hello")
    e = log.add(
        "tool_result",
        {
            "call_id": "call_9",
            "is_error": False,
            "completeness": "complete",
            "preview": "x",
            "origin": "executed",
        },
        actor="tool",
    )
    negative(
        root,
        "tool-result-without-call",
        "A tool_result for a call_id with no pending tool_call: invalid_transition.",
        log,
        ("invalid_transition", num(e["seq"])),
    )

    log = Log()
    started(log, [EMAIL])
    user(log, "Email bob.")
    r = log.model_request()
    log.model_response(
        r,
        [
            {
                "type": "tool_use",
                "call_id": "call_1",
                "name": "send_email",
                "input": EMAIL_IN,
            }
        ],
        "tool_use",
        tokens(80, 25),
    )
    log.tool_call(r, "call_1", "send_email", EMAIL_IN)
    e = log.add("effect_begin", {"call_id": "call_1", "attempt": 1})
    negative(
        root,
        "effect-begin-without-permission",
        "effect_begin with no permission_decision allow (or consumed approval) for the call: "
        "invalid_transition.",
        log,
        ("invalid_transition", num(e["seq"])),
    )

    log = base_simple()
    user(log, "Again.")
    r = log.model_request()
    log.model_response(
        r,
        [
            {
                "type": "tool_use",
                "call_id": "call_1",
                "name": "read_file",
                "input": {"path": "README.md"},
            }
        ],
        "tool_use",
        tokens(10, 1),
    )
    e = log.tool_call(r, "call_1", "read_file", {"path": "README.md"})
    negative(
        root,
        "duplicate-call-id",
        "A second tool_call reuses call_id call_1 on the same branch: invalid_transition.",
        log,
        ("invalid_transition", num(e["seq"])),
    )

    log = base_simple()
    summary = log.art(b"User asked about README.", "text/plain")
    first, mid = log.events[1], log.events[4]
    e = log.add(
        "compacted",
        {
            "from_seq": first["seq"],
            "to_seq": mid["seq"],
            "from_event_id": first["event_id"],
            "to_event_id": mid["event_id"],
            "summary_ref": summary,
        },
    )
    negative(
        root,
        "compaction-splits-tool-pair",
        "compacted ends between a tool_call and its result, so to_seq is not a step boundary "
        "and the pair would be split: invalid_transition. Mid-turn compaction at a step "
        "boundary is allowed.",
        log,
        ("invalid_transition", num(e["seq"])),
    )

    _build_turn_rules(root)


def _build_turn_rules(root: pathlib.Path) -> None:
    """Turn, cancellation, approval and late-result rules."""
    log = Log()
    started(log, [EMAIL])
    effect_call(log, EMAIL, EMAIL_IN, "Email bob.")
    log.add("effect_begin", {"call_id": "call_1", "attempt": 1})
    cr = log.add("cancel_requested", {"scope": "turn"}, actor="user", principal=ALICE)
    e = log.add("cancelled", {"request_event_id": cr["event_id"]})
    negative(
        root,
        "cancelled-with-unsettled-effect",
        "cancelled written while an effect is begun and unsettled (it must park instead): "
        "invalid_transition.",
        log,
        ("invalid_transition", num(e["seq"])),
    )

    log = Log()
    started(log, [READ_FILE])
    user(log, "hello")
    e = user(log, "hello again")
    negative(
        root,
        "user-input-inside-open-turn",
        "A second user_input while a turn is open (new input mid-turn must be steer): "
        "invalid_transition.",
        log,
        ("invalid_transition", num(e["seq"])),
    )

    log = Log()
    started(log, [EMAIL])
    effect_call(
        log,
        EMAIL,
        EMAIL_IN,
        "Email bob.",
        permission={"decision": "ask", "source": "policy"},
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
    e = log.add(
        "approval_granted",
        {
            "challenge_id": ch,
            "call_id": "call_1",
            "args_hash": sha(canonical({**EMAIL_IN, "to": "eve@example.com"})),
        },
        actor="approver",
        principal={"issuer": "api", "tenant": "acme", "subject": "carol"},
    )
    negative(
        root,
        "approval-args-mismatch",
        "approval_granted names an args_hash that differs from the open challenge: "
        "approval_mismatch.",
        log,
        ("approval_mismatch", num(e["seq"])),
    )

    log = Log()
    started(log, [TESTS])
    user(log, "Run the tests.")
    r = log.model_request()
    log.model_response(
        r,
        [{"type": "tool_use", "call_id": "call_1", "name": "run_tests", "input": {}}],
        "tool_use",
        tokens(30, 5),
    )
    log.tool_call(r, "call_1", "run_tests", {})
    log.add("permission_decision", {"call_id": "call_1", **ALLOW})
    log.add(
        "tool_result",
        {
            "call_id": "call_1",
            "is_error": False,
            "completeness": "complete",
            "preview": "12 passed",
            "origin": "executed",
        },
        actor="tool",
    )
    e = log.add(
        "tool_result_late",
        {
            "call_id": "call_1",
            "is_error": False,
            "completeness": "complete",
            "preview": "12 passed",
        },
        actor="tool",
    )
    negative(
        root,
        "late-result-without-placeholder",
        "tool_result_late for a call whose tool_result was not a deferred placeholder: "
        "invalid_transition.",
        log,
        ("invalid_transition", num(e["seq"])),
    )
