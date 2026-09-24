# pyright: strict
"""Team wire vector cases for mail, asks, operator requests and policy decisions."""

from __future__ import annotations

from .common import ALICE, sha
from .team_pieces import (
    LEAD_BRANCH,
    LOG_THREAD,
    MEMBER_BRANCH,
    REQUEST,
    RESEARCHER,
    body,
    completed,
    failed,
    provenance,
)
from .team_wire_lines import (
    ASK,
    E2,
    FROM_OP,
    HOST,
    MONITOR,
    POLICY,
    PROV,
    SETTLED,
    TASK,
    line,
    mail,
    sent,
)

# (name, line, valid)
MAIL_CASES: tuple[tuple[str, str, bool], ...] = (
    ("message_sent task", sent(TASK), True),
    (
        "message_sent message from the operator",
        sent(mail("message", FROM_OP, body=body("Keep it short."))),
        True,
    ),
    ("message_sent ask", sent(ASK), True),
    (
        "message_sent ask without a deadline",
        sent({k: v for k, v in ASK.items() if k != "deadline"}),
        False,
    ),
    (
        "message_sent reply to the team log",
        sent(
            mail(
                "reply", RESEARCHER, "team_log", ask_id=f"{LEAD_BRANCH}:c1", body=body("Batteries.")
            )
        ),
        True,
    ),
    (
        "message_sent reply from the operator",
        sent(mail("reply", FROM_OP, ask_id="x:c1", body=body("Batteries."))),
        False,
    ),
    (
        "message_sent message to the team log",
        sent(mail("message", RESEARCHER, "team_log", body=body("hi"))),
        False,
    ),
    (
        "message_sent message with a monitor_id",
        sent(mail("message", body=body("hi"), monitor_id=MONITOR)),
        False,
    ),
    ("message_sent message without a body", sent(mail("message")), False),
    ("message_sent cancel", sent(mail("cancel")), True),
    ("message_sent cancel with a body", sent(mail("cancel", body=body("stop"))), False),
    ("message_sent member_settled", sent(SETTLED), True),
    (
        "message_sent member_settled with a failed result",
        sent({**SETTLED, "result": failed(RESEARCHER, "model_error")}),
        False,
    ),
    (
        "message_sent member_ended",
        sent(
            mail(
                "member_ended",
                RESEARCHER,
                "lead",
                monitor_id=MONITOR,
                result=failed(RESEARCHER, "pin_mismatch"),
            )
        ),
        True,
    ),
    (
        "message_sent member_ended with a completed result",
        sent(
            mail(
                "member_ended",
                RESEARCHER,
                "lead",
                monitor_id=MONITOR,
                result=completed(RESEARCHER, "x"),
            )
        ),
        False,
    ),
    (
        "message_sent member_parked",
        sent(
            mail(
                "member_parked", RESEARCHER, "lead", monitor_id=MONITOR, reason="awaiting_approval"
            )
        ),
        True,
    ),
    (
        "message_sent member_parked without a reason",
        sent(mail("member_parked", RESEARCHER, "lead", monitor_id=MONITOR)),
        False,
    ),
    ("message_sent bounce", sent(mail("bounce", RESEARCHER, "lead", code="stale_member")), True),
    (
        "message_sent ask bounce with the ended member's result",
        sent(
            mail(
                "bounce",
                RESEARCHER,
                "lead",
                code="member_ended",
                ask_id=f"{LEAD_BRANCH}:c1",
                result=failed(RESEARCHER, "pin_unavailable"),
            )
        ),
        True,
    ),
    (
        "mail_refused naming a mail id that is not <branch_id>:<key>",
        line("mail_refused", {"mail_id": "m:c9", "code": "member_ended"}),
        False,
    ),
    (
        "message_policy_decided naming its rule by index",
        line(
            "message_policy_decided",
            {**POLICY, "source": "message_policy", "rule": 0, "request_id": REQUEST},
        ),
        False,
    ),
    (
        "message_sent ask bounce without the ended member's result",
        sent(mail("bounce", RESEARCHER, "lead", code="member_ended", ask_id=f"{LEAD_BRANCH}:c1")),
        False,
    ),
    ("message_sent bounce without a code", sent(mail("bounce", RESEARCHER, "lead")), False),
    (
        "message_sent bounce code forbidden",
        sent(mail("bounce", RESEARCHER, "lead", code="forbidden")),
        False,
    ),
    (
        "message_sent to a member with no generation",
        sent({**TASK, "to": {"name": "researcher-1"}}),
        False,
    ),
    (
        "message_received",
        line("message_received", {"mail_id": f"{LEAD_BRANCH}:c1", "envelope": TASK}, HOST),
        True,
    ),
    (
        "message_received without a principal",
        line("message_received", {"mail_id": f"{LEAD_BRANCH}:c1", "envelope": TASK}),
        False,
    ),
    (
        "mail_refused",
        line("mail_refused", {"mail_id": f"{LEAD_BRANCH}:c1", "code": "member_ended"}),
        True,
    ),
    (
        "mail_refused code self",
        line("mail_refused", {"mail_id": f"{LEAD_BRANCH}:c1", "code": "self"}),
        False,
    ),
    (
        "ask_closed answered",
        line(
            "ask_closed",
            {
                "ask_id": f"{LEAD_BRANCH}:c1",
                "outcome": {"status": "answered", "reply": f"{MEMBER_BRANCH}:c9"},
            },
        ),
        True,
    ),
    (
        "ask_closed answered without its reply",
        line("ask_closed", {"ask_id": f"{LEAD_BRANCH}:c1", "outcome": {"status": "answered"}}),
        False,
    ),
    (
        "ask_closed timed_out",
        line("ask_closed", {"ask_id": f"{LEAD_BRANCH}:c1", "outcome": {"status": "timed_out"}}),
        True,
    ),
    (
        "ask_closed member_ended",
        line(
            "ask_closed",
            {
                "ask_id": f"{LEAD_BRANCH}:c1",
                "outcome": {
                    "status": "member_ended",
                    "result": failed(RESEARCHER, "pin_unavailable"),
                },
            },
        ),
        True,
    ),
    (
        "ask_closed needs_input",
        line("ask_closed", {"ask_id": f"{LEAD_BRANCH}:c1", "outcome": {"status": "needs_input"}}),
        False,
    ),
    (
        "operator_request",
        line(
            "operator_request",
            {
                "request_id": REQUEST,
                "op": "start",
                "principal": ALICE,
                "idempotency_key": "k1",
                "body_hash": sha(b"{}"),
                "provenance": provenance(E2, thread=LOG_THREAD),
            },
            HOST,
        ),
        True,
    ),
    (
        "operator_request op monitor",
        line(
            "operator_request",
            {
                "request_id": REQUEST,
                "op": "monitor",
                "principal": ALICE,
                "body_hash": sha(b"{}"),
                "provenance": PROV,
            },
            HOST,
        ),
        False,
    ),
    (
        "operator_refused",
        line("operator_refused", {"request_id": REQUEST, "code": "idempotency_key_reused"}),
        True,
    ),
    (
        "operator_refused busy",
        line("operator_refused", {"request_id": REQUEST, "code": "busy"}),
        False,
    ),
    (
        "message_policy_decided by the team",
        line("message_policy_decided", {**POLICY, "call_id": "c1"}),
        True,
    ),
    (
        "message_policy_decided by a rule",
        line(
            "message_policy_decided",
            {
                **POLICY,
                "source": "message_policy",
                "rule": {"from": "lead", "to": "researcher"},
                "request_id": REQUEST,
            },
        ),
        True,
    ),
    (
        "message_policy_decided by a rule without its index",
        line("message_policy_decided", {**POLICY, "source": "message_policy", "call_id": "c1"}),
        False,
    ),
    (
        "message_policy_decided naming a call and a request",
        line("message_policy_decided", {**POLICY, "call_id": "c1", "request_id": REQUEST}),
        False,
    ),
    (
        "message_policy_decided op wait",
        line("message_policy_decided", {**POLICY, "op": "wait", "call_id": "c1"}),
        False,
    ),
)
