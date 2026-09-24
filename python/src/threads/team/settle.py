"""A member's own settling append (spec/schema/README.md, "Teams", Settling): with the
turn_completed that ends its task, member_idle or member_ended, one notification per monitor row
it fires (in monitor_id order), and for an end a mail_refused plus a bounce per pending inbound
mail (in (created_at, mail_id) order) and, for a lead, one cancel per live member (in name order).
The index hooks delete each fired monitor, return each refused row and close the team in the same
transaction. Reference: spec/tools/fixtures/ops_life.py."""

import sqlite3
from dataclasses import dataclass
from typing import Final

from pydantic import JsonValue

from threads.log import MailEnvelope, MemberRef
from threads.reduce.handlers import to_json
from threads.store.lines import Draft
from threads.team.batch import Batch
from threads.team.mail import PutText, address_of, body_of, refused, sent
from threads.team.rows import (
    MemberRow,
    TeamRow,
    member_rows,
    monitors_on,
    open_asks,
    own_rows,
    pending_to,
    ref_of,
    team_row,
)


@dataclass(frozen=True, slots=True)
class Completed:
    """The task's answer: idle with its text."""

    output: str


type Settlement = Completed | dict[str, JsonValue]
"""How a member's task ended: idle with its answer, or an ended result without its member."""


@dataclass(frozen=True, slots=True)
class AppendContext:
    """The writer a team append goes through, and its batch."""

    conn: sqlite3.Connection
    batch: Batch
    thread_id: str
    branch_id: str


@dataclass(frozen=True, slots=True)
class SettleContext(AppendContext):
    """What a settling append reads besides: the turn it closes, and where big text goes."""

    provenance: JsonValue
    """The provenance of the turn the settlement closes: its notifications belong to it."""
    put: PutText


_FIRES: Final = {
    "member_idle": frozenset({"settle", "task"}),
    "member_ended": frozenset({"settle", "task", "end"}),
}


def settle(ctx: SettleContext, how: Settlement) -> None:
    """Appends the settlement to the batch; a thread that is no team member settles nothing."""
    rows = own_rows(ctx.conn, ctx.thread_id)
    primary = next((r for r in rows if r.role == "member"), rows[0] if rows else None)
    if primary is None:
        return
    team = team_row(ctx.conn, primary.team_id)
    if team is None:
        raise AssertionError(f"no teams row {primary.team_id}")
    member = ref_of(team, primary)

    def team_of(row: MemberRow) -> TeamRow:
        return team_row(ctx.conn, row.team_id) or team

    if isinstance(how, Completed):
        result: dict[str, JsonValue] = {
            "member": member,
            "status": "completed",
            "output": body_of(how.output, ctx.put),
        }
        settled = ctx.batch.add(Draft("member_idle", {"result": result}))
        for row in rows:
            _fire(ctx, team_of(row), row, "member_idle", settled, result)
        return
    result = {"member": member, **how}
    # The member's own open asks never outlive it: each closes cancelled before its end.
    closed = {str(d.data["ask_id"]) for d in ctx.batch.drafts if d.type == "ask_closed"}
    for ask_id in open_asks(ctx.conn, ctx.branch_id):
        if ask_id not in closed:
            outcome: JsonValue = {"status": "cancelled"}
            ctx.batch.add(Draft("ask_closed", {"ask_id": ask_id, "outcome": outcome}))
    settled = ctx.batch.add(Draft("member_ended", {"result": result}))
    for row in rows:
        _fire(ctx, team_of(row), row, "member_ended", settled, result)
        refuse_all(ctx, team_of(row), row, result, bounce_all=True)
        if row.role == "lead":
            _close(ctx, team_of(row), row, settled)


def _mail_id(ctx: AppendContext) -> str:
    return f"{ctx.branch_id}:{ctx.batch.next_id()}"


def _causal(ctx: AppendContext, event_id: str) -> JsonValue:
    return {"thread_id": ctx.thread_id, "event_id": event_id}


def _fire(  # noqa: PLR0913, PLR0917 - one firing: where, whose, why and what
    ctx: SettleContext,
    team: TeamRow,
    row: MemberRow,
    kind: str,
    settled: str,
    result: JsonValue,
) -> None:
    """One notification per monitor row on this generation that the event fires."""
    for m in monitors_on(ctx.conn, row.team_id, row):
        if m.kind not in _FIRES[kind]:
            continue
        env: dict[str, JsonValue] = {
            "mail_id": _mail_id(ctx),
            "kind": "member_settled" if kind == "member_idle" else "member_ended",
            "team": row.team_id,
            "from": ref_of(team, row),
            "to": address_of(ctx.conn, row.team_id, m.watcher_branch_id),
            "provenance": ctx.provenance,
            "causal": _causal(ctx, settled),
            "monitor_id": m.monitor_id,
            "result": result,
        }
        ctx.batch.add(sent(env))


def refuse_all(
    ctx: AppendContext, team: TeamRow, row: MemberRow, result: JsonValue, *, bounce_all: bool
) -> list[MailEnvelope]:
    """Every pending inbound mail of an ended member is refused under its writer: a mail_refused
    and, at its end append or for a message or an ask, a bounce to its sender carrying the
    refused mail's provenance (an ask's bounce also carries the ended member's result)."""
    taken = ctx.batch.taken()
    pending = [m for m in pending_to(ctx.conn, row.team_id, row.name) if m.mail_id not in taken]
    for mail in pending:
        why = ctx.batch.add(refused(mail.mail_id))
        if bounce_all or mail.kind in ("message", "ask"):
            ctx.batch.add(sent(_bounce(ctx, team, row, mail, why, result)))
    return pending


def _bounce(  # noqa: PLR0913, PLR0917 - a bounce: where, whose, of what, why and with what
    ctx: AppendContext,
    team: TeamRow,
    row: MemberRow,
    mail: MailEnvelope,
    why: str,
    result: JsonValue,
) -> dict[str, JsonValue]:
    sender = mail.from_
    back: JsonValue = (
        {"name": sender.name, "generation": sender.generation}
        if isinstance(sender, MemberRef)
        else "team_log"
    )
    env: dict[str, JsonValue] = {
        "mail_id": _mail_id(ctx),
        "kind": "bounce",
        "team": row.team_id,
        "from": ref_of(team, row),
        "to": back,
        "provenance": to_json(mail.provenance),
        "causal": _causal(ctx, why),
        "code": "member_ended",
    }
    if mail.kind == "ask" and isinstance(mail.ask_id, str):
        env |= {"ask_id": mail.ask_id, "result": result}
    return env


def _close(ctx: SettleContext, team: TeamRow, lead: MemberRow, settled: str) -> None:
    """A lead's end closes its team: one cancel per live member, in name order (member_rows
    reads in name order)."""
    for r in member_rows(ctx.conn, lead.team_id):
        if r.role != "member" or r.state == "ended":
            continue
        env: dict[str, JsonValue] = {
            "mail_id": _mail_id(ctx),
            "kind": "cancel",
            "team": lead.team_id,
            "from": ref_of(team, lead),
            "to": {"name": r.name, "generation": r.generation},
            "provenance": ctx.provenance,
            "causal": _causal(ctx, settled),
        }
        ctx.batch.add(sent(env))
