# pyright: strict
"""Staged `team` cases for a failed rebind (spec/schema/README.md, "Teams", "A failed rebind"):
the member's one end-of-member append, its bounces, and a parked member asker taking the ask's
bounce as control mail. The forged variant drops the ask from that bounce (rule 43)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import eid, text
from .pieces import user
from .team_pieces import (
    DEADLINE,
    LEAD,
    LEAD_BRANCH,
    MEMBER_BRANCH,
    MEMBER_THREAD,
    RESEARCHER,
    WRITER,
    WRITER_BRANCH,
    WRITER_THREAD,
    Route,
    at,
    body,
    envelope,
    failed,
    lead_log,
    provenance,
    team_log,
)
from .team_steps import answered, idle, materialize, received, start, tool, write_team

if TYPE_CHECKING:
    import pathlib

    from .jcs import JsonValue, Obj
    from .log import Log


def build(root: pathlib.Path) -> None:
    _case(root, forge=False)
    _case(root, forge=True)


def _writer_asks(writer: Log, prov: Obj) -> Obj:
    """The writer asks the starting researcher and parks on the ask."""
    c = tool(
        writer, "ask", {"to": "researcher-1", "question": "Which topic?"}, "c1", "researcher-1"
    )
    ask_id = f"{WRITER_BRANCH}:c1"
    ask = envelope(
        ask_id,
        "ask",
        Route(WRITER, "researcher-1", prov),
        at(text(c["event_id"]), WRITER_THREAD),
        ask_id=ask_id,
        deadline=DEADLINE,
        body=body("Which topic?"),
    )
    writer.add("message_sent", {"envelope": ask})
    writer.add("parked", {"address": {"kind": "ask", "id": ask_id}, "reason": "awaiting_member"})
    return ask


def _fails(
    member: Log, prov: Obj, monitor: str, pending: list[tuple[Obj, Obj]], forge: bool
) -> tuple[Obj, list[Obj]]:
    """The rebind fails: turn_completed{error}, member_ended{failed}, the task monitor's
    notification, and a mail_refused plus a bounce for each pending mail, in one append."""
    member.add("turn_completed", {"reason": "error", "code": "pin_unavailable"})
    ended = failed(RESEARCHER, "pin_unavailable")
    end_id = text(member.add("member_ended", {"result": ended})["event_id"])

    def mail_id() -> str:
        return f"{MEMBER_BRANCH}:{eid(member.seq + 1, MEMBER_BRANCH)}"

    route = Route(RESEARCHER, "lead", prov)
    notice = envelope(
        mail_id(),
        "member_ended",
        route,
        at(end_id, MEMBER_THREAD),
        monitor_id=monitor,
        result=ended,
    )
    member.add("message_sent", {"envelope": notice})
    bounces: list[Obj] = []
    for mail, sender in pending:
        why = member.add("mail_refused", {"mail_id": mail["mail_id"], "code": "member_ended"})
        extra: dict[str, JsonValue] = {}
        if mail["kind"] == "ask" and not forge:
            extra = {"ask_id": mail["ask_id"], "result": ended}
        back = Route(RESEARCHER, text(sender["name"]), prov)
        bounce = envelope(
            mail_id(),
            "bounce",
            back,
            at(text(why["event_id"]), MEMBER_THREAD),
            code="member_ended",
            **extra,
        )
        member.add("message_sent", {"envelope": bounce})
        bounces.append(bounce)
    return notice, bounces


def _case(root: pathlib.Path, *, forge: bool) -> None:
    lead = lead_log()
    root_event = text(user(lead, "Research batteries, then write it up.")["event_id"])
    prov = provenance(root_event)
    researcher_started, researcher_task = start(lead, root_event, RESEARCHER, "c1")
    writer_started, writer_task = start(lead, root_event, WRITER, "c2", WRITER_THREAD)
    c = tool(lead, "send", {"to": "researcher-1", "text": "Keep it short."}, "c3", "researcher-1")
    note = envelope(
        f"{LEAD_BRANCH}:c3",
        "message",
        Route(LEAD, "researcher-1", prov),
        at(text(c["event_id"])),
        body=body("Keep it short."),
    )
    lead.add("message_sent", {"envelope": note})
    answered(lead, "c3", {"id": note["mail_id"], "status": "sent"})
    idle(lead, LEAD, "Started the researcher and the writer.")

    writer = materialize(writer_started, writer_task, "writer", (WRITER_BRANCH, WRITER_THREAD))
    ask = _writer_asks(writer, prov)

    researcher = materialize(researcher_started, researcher_task)
    monitor = f"{LEAD_BRANCH}:{researcher_started}:task"
    notice, bounces = _fails(researcher, prov, monitor, [(note, LEAD), (ask, WRITER)], forge)
    logs = {"lead": lead, "researcher": researcher, "team": team_log(), "writer": writer}
    if forge:
        bounce_seq = researcher.seq
        write_team(
            root,
            "team-ask-bounce-without-ask-rejected",
            "Rule 43: researcher-1 refuses the writer's open ask but its bounce names no ask_id "
            "(and carries no result), so the parked asker could never close the ask. Only the "
            "sender's mail shows the refused mail was an ask: invalid_transition at the bounce, "
            "in the researcher's log.",
            logs,
            {"code": "invalid_transition", "seq": bounce_seq, "log": "researcher"},
        )
        return

    # The parked writer takes the ask's bounce as control mail: it closes the ask member_ended,
    # resumes and records the ask call's one result; the bounce opens no turn.
    got = received(writer, bounces[1])
    ended = bounces[1]["result"]
    writer.add(
        "ask_closed",
        {"ask_id": ask["ask_id"], "outcome": {"status": "member_ended", "result": ended}},
    )
    writer.add(
        "resumed",
        {"address": {"kind": "ask", "id": ask["ask_id"]}, "cause_event_id": got["event_id"]},
    )
    answered(writer, "c1", {"ask_id": ask["ask_id"], "result": ended, "status": "member_ended"})

    # The idle lead: the task monitor's member_ended opens its turn; the message's bounce joins.
    received(lead, notice)
    received(lead, bounces[0])
    lead.model_request()
    write_team(
        root,
        "team-failed-rebind-bounces",
        "The lead starts researcher-1 and writer-1 and sends the researcher a message. The "
        "writer, a member, asks the still-starting researcher and parks on the ask. The "
        "researcher's rebind fails: one append opens its log with the task's user_input, "
        "turn_completed{error: pin_unavailable}, member_ended{failed}, one member_ended "
        "notification for the lead's task monitor, and a mail_refused plus a bounce for the "
        "message and for the ask; the ask's bounce carries the ask_id and the result. The parked "
        "writer consumes that bounce as control mail: ask_closed{member_ended}, resumed and the "
        "ask call's one tool_result, with no turn opened by the bounce and no timeout. The idle "
        "lead wakes on the task notification. The index rebuilds both tasks consumed, both "
        "refused mails returned, the ask closed member_ended and only the writer's task monitor "
        "left.",
        logs,
    )
