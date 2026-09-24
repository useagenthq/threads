# pyright: strict
"""Semantic rules 31-42 (spec/schema/README.md, "Semantic rules"): one staged reduce case per
rule, whose last event breaks it. Rule 43 is cross-log (team_replay, team_rebind); 44 and 45
are in team_bindings. Staged until the Teams build implements the rules
(spec/conformance/README.md, "Staged cases")."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, eid, obj
from .pieces import reject, user
from .team_pieces import (
    DEADLINE,
    LEAD,
    LEAD_BRANCH,
    LEAD_THREAD,
    MEMBER_BRANCH,
    RESEARCHER,
    TEAM,
    Route,
    at,
    body,
    envelope,
    failed,
    lead_log,
    member_log,
    provenance,
    team_log,
)

if TYPE_CHECKING:
    import pathlib

    from .jcs import Obj
    from .log import Log

FAM = "agents_teams"
BOB: Obj = {"issuer": "api", "tenant": "acme", "subject": "bob"}
ROOT = eid(9)  # the lead run's user_input, in the lead's log


def _mail(mail_id: str, principal: Obj = ALICE, text_: str = "Prices fell.") -> Obj:
    prov = provenance(ROOT, principal)
    return envelope(mail_id, "message", Route(RESEARCHER, "lead", prov), at(ROOT), body=body(text_))


def _receive(log: Log, env: Obj, mail_id: str | None = None) -> Obj:
    data: Obj = {"mail_id": mail_id or env["mail_id"], "envelope": env}
    sender = obj(obj(env["provenance"])["principal"])  # the receipt acts for its provenance
    return log.add("message_received", data, actor="host", principal=sender)


def _member(log_first_input: bool = True) -> Log:
    log = member_log(eid(4))
    if log_first_input:
        data: Obj = {
            "source": "team_task",
            "text": "Topic: batteries.",
            "mail_id": f"{LEAD_BRANCH}:c1",
        }
        log.add("user_input", data, actor="host", principal=ALICE)
    return log


def build(root: pathlib.Path) -> None:
    for name, desc, log in (*_mail_cases(), *_lifecycle_cases()):
        reject(root, (name, FAM, desc), log)
    log = lead_log()
    _receive(log, _mail(f"{MEMBER_BRANCH}:c1"))
    _receive(log, _mail(f"{MEMBER_BRANCH}:c1"))
    reject(root, ("team-receipt-twice-rejected", FAM, "Rule 31: one mail is received twice."), log)


def _mail_cases() -> list[tuple[str, str, Log]]:
    """Rules 31-36: receipts, wakes, the team log, batches, replies and asks."""
    out: list[tuple[str, str, Log]] = []

    log = lead_log()
    _receive(log, _mail(f"{MEMBER_BRANCH}:c1"), mail_id=f"{MEMBER_BRANCH}:c2")
    out.append(
        (
            "team-receipt-mail-id-mismatch-rejected",
            "Rule 31: a receipt's mail_id is not its envelope's.",
            log,
        )
    )

    log = team_log()
    log.add("user_input", {"source": "api", "text": "hi"}, actor="user", principal=ALICE)
    out.append(("team-log-foreign-event-rejected", "Rule 33: a team log takes a user_input.", log))

    log = lead_log()
    log.add("team_opened", {"team": TEAM, "lead": LEAD, "lead_thread_id": LEAD_THREAD})
    out.append(
        (
            "team-opened-not-first-rejected",
            "Rule 33: team_opened after a branch's first event.",
            log,
        )
    )

    log = _member(log_first_input=False)
    task = envelope(
        f"{LEAD_BRANCH}:c1",
        "task",
        Route(LEAD, "researcher-1", provenance(ROOT)),
        at(ROOT),
        body=body("Topic: batteries."),
    )
    _receive(log, task)
    out.append(
        (
            "team-task-as-message-rejected",
            "Rule 34: a task arrives as message_received, not as user_input{team_task}.",
            log,
        )
    )

    log = lead_log()
    _receive(log, _mail(f"{MEMBER_BRANCH}:c1"))
    _receive(log, _mail(f"{MEMBER_BRANCH}:c2", BOB, "Sales rose."))
    out.append(
        (
            "team-mixed-provenance-turn-rejected",
            "Rule 34: ordinary mail from another principal joins an open turn.",
            log,
        )
    )

    log = _member()
    reply = envelope(
        f"{MEMBER_BRANCH}:c2",
        "reply",
        Route(RESEARCHER, "lead", provenance(ROOT)),
        at(ROOT),
        ask_id=f"{LEAD_BRANCH}:c9",
        body=body("Batteries."),
    )
    log.add("message_sent", {"envelope": reply})
    out.append(
        (
            "team-reply-unknown-ask-rejected",
            "Rule 35: a reply names an ask this log never received.",
            log,
        )
    )

    log = lead_log()
    user(log, "Ask the researcher.")
    ask = envelope(
        f"{LEAD_BRANCH}:c2",
        "ask",
        Route(LEAD, "researcher-1", provenance(ROOT)),
        at(ROOT),
        ask_id=f"{LEAD_BRANCH}:c2",
        deadline=DEADLINE,
        body=body("Which topic?"),
    )
    log.add("message_sent", {"envelope": ask})
    log.add("ask_closed", {"ask_id": f"{LEAD_BRANCH}:c2", "outcome": {"status": "timed_out"}})
    log.add("ask_closed", {"ask_id": f"{LEAD_BRANCH}:c2", "outcome": {"status": "timed_out"}})
    out.append(("ask-closed-twice-rejected", "Rule 36: an ask closes twice.", log))

    return out


def _lifecycle_cases() -> list[tuple[str, str, Log]]:
    """Rules 37-42: member ends, idles, waits, parks, first inputs and operator requests."""
    out: list[tuple[str, str, Log]] = []

    log = _member()
    log.add("turn_completed", {"reason": "error", "code": "pin_unavailable"})
    log.add("member_ended", {"result": failed(RESEARCHER, "pin_unavailable")})
    note = envelope(
        f"{LEAD_BRANCH}:c3",
        "message",
        Route(LEAD, "researcher-1", provenance(ROOT)),
        at(ROOT),
        body=body("Hurry."),
    )
    _receive(log, note)
    out.append(
        ("member-ended-then-input-rejected", "Rule 37: an ended member's log opens a turn.", log)
    )

    log = _member()
    log.add(
        "member_idle",
        {"result": {"member": RESEARCHER, "status": "completed", "output": {"text": "Done."}}},
    )
    out.append(
        (
            "member-idle-without-turn-end-rejected",
            "Rule 38: member_idle with the task's turn still open.",
            log,
        )
    )

    log = lead_log()
    user(log, "Wait for them.")
    log.add(
        "wait_finished",
        {
            "wait_id": f"{LEAD_BRANCH}:c9",
            "finished": [],
            "parked": [],
            "pending": [],
            "timed_out": True,
        },
    )
    out.append(
        (
            "wait-finished-unknown-wait-rejected",
            "Rule 39: wait_finished names no wait of this log.",
            log,
        )
    )

    log = lead_log()
    user(log, "Ask the researcher.")
    log.add(
        "parked",
        {"address": {"kind": "ask", "id": f"{LEAD_BRANCH}:c9"}, "reason": "awaiting_member"},
    )
    out.append(("park-unknown-ask-rejected", "Rule 40: a park on an ask this log never sent.", log))

    log = _member(log_first_input=False)
    log.add("user_input", {"source": "api", "text": "hi"}, actor="user", principal=ALICE)
    out.append(
        (
            "team-task-input-not-first-rejected",
            "Rule 41: a member's first input is not its task.",
            log,
        )
    )

    log = team_log()
    log.add(
        "operator_refused",
        {"request_id": "0192d000-0000-7000-8000-000000000009", "code": "forbidden"},
    )
    out.append(
        (
            "operator-refused-without-request-rejected",
            "Rule 42: operator_refused names no operator_request.",
            log,
        )
    )
    return out
