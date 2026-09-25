import type { EventOf } from "../fold/state";
import type { MailEnvelope } from "../log";
import {
  addressed,
  type CallContext,
  callerOf,
  callMailId,
  causalOf,
  decide,
  isRefusal,
  recorded,
  refusal,
} from "./call";
import {
  type CloseContext,
  committedNotices,
  completeAsk,
  finishWait,
} from "./close";
import { received, sent } from "./mail";
import { memberNamed, refOf } from "./rows";
import { askOpen, openWaits, parksNow } from "./view";

// cancel: durable intent, then application (spec/schema/README.md, "Teams"; design §4.14). The
// request is a cancel mail in the canceller's log; the target's writer applies it as control
// mail, whatever it is doing: its receipt and today's barrier, cancel_requested{scope: tree},
// with every park it leaves released in the same append. The barrier rules then end the member
// cancelled. Reference: spec/tools/fixtures/ops_send.py (cancel) and ops_consume.py.

/** The members a caller started: their member_started is in its own log. */
function started(ctx: CallContext, name: string): boolean {
  return ctx.chain.events.some(
    (l) =>
      l.kind === "event" &&
      l.event.type === "member_started" &&
      l.event.data.member.name === name,
  );
}

/**
 * cancel's request: the target's starter may (Phase 1 policy); the member is known at its
 * generation and not ended. Returns what the call recorded.
 */
export async function cancel(
  ctx: CallContext,
  args: { readonly member: string },
): Promise<unknown> {
  const caller = await callerOf(ctx);
  if (caller === undefined) throw new Error("a team tool call outside a team");
  const known = await memberNamed(ctx.tx, caller.team.team_id, args.member);
  const denied = decide(
    ctx,
    "cancel",
    args.member,
    known !== undefined && started(ctx, args.member),
  );
  if (denied !== undefined) return recorded(ctx, denied);
  const row = await addressed(ctx, caller, args.member);
  if (isRefusal(row)) return recorded(ctx, row);
  if (row.state === "ended") return recorded(ctx, refusal("member_ended"));
  ctx.batch.add(
    sent({
      mail_id: callMailId(ctx),
      kind: "cancel",
      team: caller.team.team_id,
      from: caller.ref,
      to: { name: row.name, generation: row.generation },
      provenance: caller.provenance,
      causal: causalOf(ctx),
    }),
  );
  return recorded(ctx, {
    member: refOf(caller.team, row),
    status: "cancel_requested",
  });
}

type Principal = EventOf<"cancel_requested">["actor"]["principal"];

/**
 * Applies a cancel: its receipt and the barrier, then every park it leaves released: an open ask
 * completes by ask.complete's decision (a pending reply or bounce still wins, else cancelled), a
 * wait takes the notices already committed for it and finishes with what settled, and a member
 * park resumes.
 */
export async function applyCancel(
  ctx: CloseContext,
  env: MailEnvelope,
): Promise<void> {
  const got = ctx.batch.add(received(env));
  const principal: Principal = env.provenance.principal;
  ctx.batch.add({
    type: "cancel_requested",
    type_version: 1,
    critical: true,
    actor: { kind: "host", principal },
    data: { scope: "tree" },
  });
  for (const park of parksNow(ctx.chain, ctx.batch)) {
    if (
      park.kind === "ask" &&
      (await askOpen(ctx.tx, ctx.branchId, park.id, ctx.batch))
    )
      await completeAsk(ctx, park.id, { cancelled: true, due: false });
    else if (
      park.kind === "wait" &&
      openWaits(ctx.chain, ctx.batch).has(park.id)
    ) {
      await committedNotices(ctx, park.id);
      await finishWait(ctx, park.id, { cause: got, deadline: true });
    } else if (park.kind === "member")
      ctx.batch.add({
        type: "resumed",
        type_version: 1,
        critical: true,
        actor: { kind: "host" },
        data: { address: park, cause_event_id: got },
      });
  }
}
