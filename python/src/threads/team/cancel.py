"""cancel: durable intent, then application (spec/schema/README.md, "Teams"; design §4.14). The
request is a cancel mail in the canceller's log; the target's writer applies it as control mail,
whatever it is doing: its receipt and today's barrier, cancel_requested{scope: tree}, with every
park it leaves released in the same append. The barrier rules then end the member cancelled.
Reference: spec/tools/fixtures/ops_send.py (cancel) and ops_consume.py. Mirrors TypeScript's
team/cancel.ts."""

from pydantic import JsonValue

from threads.log import MailEnvelope
from threads.reduce.handlers import to_json
from threads.store.lines import Draft
from threads.team.call import CallContext, Refusal, call_request, named
from threads.team.close import CloseContext, committed_notices, complete_ask, finish_wait
from threads.team.mail import received, sent
from threads.team.request import Request, Target
from threads.team.rows import ref_of
from threads.team.view import ask_open, open_waits, parks_now


def cancel(ctx: CallContext, member: str) -> JsonValue:
    """The model's cancel: its call as the request, the member it names as the target."""
    return request_cancel(call_request(ctx), named(ctx, member))


def request_cancel(req: Request, to: Target) -> dict[str, JsonValue]:
    """cancel's request, for a model call or an operator request: the policy (a model's cancel is
    its target's starter's; an operator's, the team's tenant's), then the member known at its
    generation and not ended; then the cancel mail."""
    denied = req.decide("cancel", to.name)
    if denied is not None:
        return req.refuse(denied)
    row = to.row()
    if isinstance(row, Refusal):
        return req.refuse(row)
    if row.state == "ended":
        return req.refuse(Refusal("member_ended"))
    env: dict[str, JsonValue] = {
        "mail_id": req.mail_id,
        "kind": "cancel",
        "team": req.team.team_id,
        "from": req.sender,
        "to": {"name": row.name, "generation": row.generation},
        "provenance": req.provenance,
        "causal": req.causal,
    }
    req.batch.add(sent(env))
    return req.done({"member": ref_of(req.team, row), "status": "cancel_requested"})


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
