"""cancel: durable intent, then application (spec/schema/README.md, "Teams"; design §4.14). The
request is a cancel mail in the canceller's log; the target's writer applies it as control mail,
whatever it is doing: its receipt and today's barrier, cancel_requested{scope: tree}, with every
park it leaves released in the same append. The barrier rules then end the member cancelled.
Reference: spec/tools/fixtures/ops_send.py (cancel) and ops_consume.py. Mirrors TypeScript's
team/cancel.ts."""

from pydantic import JsonValue

from threads.log import MailEnvelope, MemberStartedEvent
from threads.reduce.handlers import to_json
from threads.store.lines import Draft
from threads.team.call import (
    CallContext,
    Refusal,
    addressed,
    call_mail_id,
    caller_of,
    causal_of,
    decide,
    recorded,
)
from threads.team.close import CloseContext, committed_notices, complete_ask, finish_wait
from threads.team.mail import received, sent
from threads.team.rows import member_named, ref_of
from threads.team.view import ask_open, open_waits, parks_now


def _started(ctx: CallContext, name: str) -> bool:
    """The caller started the member: its member_started is in the caller's own log."""
    return any(
        isinstance(e, MemberStartedEvent) and e.data.member.name == name for e in ctx.fold.events
    )


def cancel(ctx: CallContext, member: str) -> JsonValue:
    """cancel's request: the target's starter may (Phase 1 policy); the member is known at its
    generation and not ended. Returns what the call recorded."""
    caller = caller_of(ctx)
    known = member_named(ctx.conn, caller.team.team_id, member)
    denied = decide(ctx, "cancel", member, allow=known is not None and _started(ctx, member))
    if denied is not None:
        return recorded(ctx, denied)
    row = addressed(ctx, caller, member)
    if isinstance(row, Refusal):
        return recorded(ctx, row)
    if row.state == "ended":
        return recorded(ctx, Refusal("member_ended"))
    env: dict[str, JsonValue] = {
        "mail_id": call_mail_id(ctx),
        "kind": "cancel",
        "team": caller.team.team_id,
        "from": caller.ref,
        "to": {"name": row.name, "generation": row.generation},
        "provenance": caller.provenance,
        "causal": causal_of(ctx),
    }
    ctx.batch.add(sent(env))
    return recorded(ctx, {"member": ref_of(caller.team, row), "status": "cancel_requested"})


def apply_cancel(ctx: CloseContext, env: MailEnvelope) -> None:
    """Applies a cancel: its receipt and the barrier, then every park it leaves released: an
    open ask completes by ask.complete's decision (a pending reply or bounce still wins, else
    cancelled), a wait takes the notices already committed for it and finishes with what settled,
    and a member park resumes."""
    got = ctx.batch.add(received(env))
    actor: dict[str, JsonValue] = {
        "kind": "host",
        "principal": to_json(env.provenance.principal),
    }
    ctx.batch.add(Draft("cancel_requested", {"scope": "tree"}, actor))
    for park in parks_now(ctx.fold, ctx.batch):
        if park.kind == "ask" and ask_open(ctx.conn, ctx.branch_id, park.id, ctx.batch):
            complete_ask(ctx, park.id, cancelled=True, due=False)
        elif park.kind == "wait" and park.id in open_waits(ctx.fold, ctx.batch):
            committed_notices(ctx, park.id)
            finish_wait(ctx, park.id, cause=got, deadline=True)
        elif park.kind == "member":
            data: dict[str, JsonValue] = {"address": to_json(park), "cause_event_id": got}
            ctx.batch.add(Draft("resumed", data))
