"""cancel: durable intent, then application (spec/schema/README.md, "Teams"; design §4.14). The
request is a cancel mail in the canceller's log; the target's writer applies it as control mail,
whatever it is doing: its receipt and today's barrier, cancel_requested{scope: tree}, with every
park it leaves released in the same append. The barrier rules then end the member cancelled.
Reference: spec/tools/fixtures/ops_send.py (cancel) and ops_consume.py. Mirrors TypeScript's
team/cancel.ts."""

from pydantic import JsonValue

from threadsai.log import MailEnvelope
from threadsai.reduce.handlers import to_json
from threadsai.store.lines import Draft
from threadsai.team.call import CallContext, Refusal, call_request, named
from threadsai.team.close import CloseContext, committed_notices, complete_ask, finish_wait
from threadsai.team.mail import received, sent
from threadsai.team.provenance import turn_provenance
from threadsai.team.request import Request, Target
from threadsai.team.rows import ref_of
from threadsai.team.settle import SettleContext, settle
from threadsai.team.view import ask_open, open_waits, parks_now


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
    and a member park resumes.

    A host member with no turn open ends here too (Teams Phase 2, E). A member with a task always
    has a turn for the barrier to close, so its end follows from that turn; a host member idles
    between callers, and nothing would ever close a barrier it took while idle. This is the same
    append §4.14 describes for a cancel that reaches a member before it starts."""
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
    _ended_idle(ctx, got)


def _ended_idle(ctx: CloseContext, cause: str) -> None:
    """An idle host member's end, in the cancel's own append: the barrier's answer and the end."""
    if not ctx.fold.host.host_member or ctx.fold.in_turn:
        return
    ctx.batch.add(Draft("cancelled", {"request_event_id": cause}))
    cancelled: dict[str, JsonValue] = {"status": "cancelled"}
    settle(
        SettleContext(
            ctx.conn,
            ctx.batch,
            ctx.thread_id,
            ctx.branch_id,
            turn_provenance(ctx.conn, ctx.fold.events),
            _no_text,
            tuple(ctx.fold.host.turn_asks),
        ),
        cancelled,
    )


def _no_text(_text: str) -> JsonValue:
    raise AssertionError("a cancelled member's result has no text")
