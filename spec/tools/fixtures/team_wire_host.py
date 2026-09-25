# pyright: strict
"""Team wire vector cases for Teams Phase 2 (spec/schema/README.md, "Teams Phase 2"): the host
team's team_opened, host members' starts and threads, caller addresses, turn failures and
supervisor_decided. Each is admitted or refused by the line schema alone."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .host_pieces import HOST_TEAM, POLICY, billing, caller, host_start
from .team_pieces import DEADLINE, LEAD, LEAD_BRANCH, LEAD_THREAD
from .team_wire_lines import BUDGET, E1, PARENT, PROV, line

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

ERROR: Obj = {"code": "model_error", "message": "billing's turn failed"}
HOST_ACTOR: Obj = {
    "kind": "host",
    "principal": {"issuer": "api", "tenant": "acme", "subject": "alice"},
}
ASK_ID = f"{LEAD_BRANCH}:c1"
PIN: Obj = {
    "agent_name": "billing",
    "config_hash": "b" * 64,
    "model": {"provider": "scripted", "name": "s"},
    "model_params": {},
    "adapter": {"name": "scripted", "version": "1", "settings": {}},
    "instructions": "",
    "tools": [],
}
HOST_OF: Obj = {"team": HOST_TEAM, "name": "billing", "generation": 1}


def _env(kind: str, frm: Obj, to: JsonValue, **k: JsonValue) -> Obj:
    return {
        "mail_id": f"{LEAD_BRANCH}:c1",
        "kind": kind,
        "team": HOST_TEAM,
        "from": frm,
        "to": to,
        "provenance": PROV,
        "causal": {"thread_id": LEAD_THREAD, "event_id": E1},
        **k,
    }


def _sent(env: Obj) -> str:
    return line("message_sent", {"envelope": env})


MEMBER: Obj = {"name": "billing", "generation": 1}
CALLER_ASK = _env("ask", caller(), MEMBER, ask_id=ASK_ID, deadline=DEADLINE, body={"text": "?"})
REPLY = _env("reply", billing(), caller(), ask_id=ASK_ID, body={"text": "Paid."})
BOUNCE = _env("bounce", billing(), caller(), ask_id=ASK_ID, code="turn_failed", error=ERROR)
DECIDED: Obj = {
    "member": billing(),
    "ended": {"branch_id": LEAD_BRANCH, "seq": 3},
    "action": "restart",
    "restarts_in_window": 0,
    "policy": POLICY,
}
DONE: Obj = {"member": billing(), "status": "completed", "output": {"text": "Paid."}}

# (name, line, valid)
HOST_CASES: tuple[tuple[str, str, bool], ...] = (
    (
        "team_opened of a host team",
        line("team_opened", {"team": HOST_TEAM, "kind": "host", "tenant": "acme"}),
        True,
    ),
    (
        "team_opened of a host team without its tenant",
        line("team_opened", {"team": HOST_TEAM, "kind": "host"}),
        False,
    ),
    (
        "team_opened of a host team with a lead",
        line(
            "team_opened",
            {"team": HOST_TEAM, "kind": "host", "tenant": "acme", "lead": LEAD},
        ),
        False,
    ),
    (
        "team_opened of a lead team with a tenant",
        line(
            "team_opened",
            {"team": HOST_TEAM, "lead": LEAD, "lead_thread_id": LEAD_THREAD, "tenant": "acme"},
        ),
        False,
    ),
    ("member_started of a host member", line("member_started", host_start(1)), True),
    (
        "member_started of a host member restarting generation 1",
        line("member_started", host_start(2, restart_of=1)),
        True,
    ),
    (
        "member_started of a host member with a parent",
        line("member_started", host_start(1, parent=PARENT)),
        False,
    ),
    (
        "member_started of a host member with a budget",
        line("member_started", host_start(1, budget={"max_turns": 4})),
        False,
    ),
    (
        "member_started with restart_of but no host_member",
        line(
            "member_started",
            {**{k: v for k, v in host_start(2).items() if k != "host_member"}, "restart_of": 1},
        ),
        False,
    ),
    (
        "thread_started of a host member",
        line("thread_started", {**PIN, "host_member": HOST_OF}),
        True,
    ),
    (
        "thread_started of a host member with a parent",
        line("thread_started", {**PIN, "host_member": HOST_OF, "parent": PARENT}),
        False,
    ),
    ("a caller's ask", _sent(CALLER_ASK), True),
    ("a caller's reply", _sent({**CALLER_ASK, "kind": "reply"}), False),
    ("a host member's reply to a caller", _sent(REPLY), True),
    ("a host member's message to a caller", _sent({**REPLY, "kind": "message"}), False),
    ("a turn_failed bounce", _sent(BOUNCE), True),
    (
        "a turn_failed bounce to a member",
        _sent({**BOUNCE, "to": {"name": "ledger", "generation": 1}}),
        True,
    ),
    (
        "a receipt to a caller",
        line("message_received", {"mail_id": REPLY["mail_id"], "envelope": REPLY}, HOST_ACTOR),
        True,
    ),
    (
        "a turn_failed bounce without its error",
        _sent({k: v for k, v in BOUNCE.items() if k != "error"}),
        False,
    ),
    ("a turn_failed bounce with a result", _sent({**BOUNCE, "result": DONE}), False),
    ("an error on a member_ended bounce", _sent({**BOUNCE, "code": "member_ended"}), False),
    ("member_idle{turn_failed}", line("member_idle", {"turn_failed": ERROR}), True),
    (
        "member_idle with both result and turn_failed",
        line("member_idle", {"result": DONE, "turn_failed": ERROR}),
        False,
    ),
    ("member_idle with neither", line("member_idle", {}), False),
    (
        "ask_closed failed",
        line("ask_closed", {"ask_id": ASK_ID, "outcome": {"status": "failed", "error": ERROR}}),
        True,
    ),
    ("supervisor_decided", line("supervisor_decided", DECIDED), True),
    (
        "supervisor_decided with a window under one second",
        line("supervisor_decided", {**DECIDED, "policy": {**POLICY, "within_ms": 999}}),
        False,
    ),
    (
        "budget_exceeded of a hop cap",
        line("budget_exceeded", {**BUDGET, "scope": "hop"}),
        True,
    ),
)
