import type { EventOf } from "../fold/state";
import type { MailEnvelope, MemberRef } from "../log";
import {
  type CallContext,
  callRequest,
  named,
  type Refused,
  refusal,
} from "./call";
import {
  type CloseContext,
  committedNotices,
  completeAsk,
  finishWait,
} from "./close";
import { received, sent } from "./mail";
import type { Request, Target } from "./request";
import { refOf } from "./rows";
import { askOpen, openWaits, parksNow } from "./view";

// cancel: durable intent, then application (spec/schema/README.md, "Teams"; design §4.14). The
// request is a cancel mail in the canceller's log; the target's writer applies it as control
// mail, whatever it is doing: its receipt and today's barrier, cancel_requested{scope: tree},
// with every park it leaves released in the same append. The barrier rules then end the member
// cancelled. Reference: spec/tools/fixtures/ops_send.py (cancel) and ops_consume.py.

/** A cancel accepted: durable, applied at the member's next step. */
export type CancelRequested = {
  readonly member: MemberRef;
  readonly status: "cancel_requested";
};

/** The model's cancel: its call as the request, the member it names as the target. */
export async function cancel(
  ctx: CallContext,
  args: { readonly member: string },
): Promise<CancelRequested | Refused> {
  return requestCancel(await callRequest(ctx), named(ctx, args.member));
}

/**
 * cancel's request, for a model call or an operator request: the policy (a model's cancel is its
 * target's starter's; an operator's, the team's tenant's), then the member known at its
 * generation and not ended; then the cancel mail.
 */
export async function requestCancel(
  req: Request,
  to: Target,
): Promise<CancelRequested | Refused> {
  const denied = req.decide("cancel", to.name);
  if (denied !== undefined) return req.refuse(denied);
  const row = await to.row();
  if ("refused" in row) return req.refuse(row);
  if (row.state === "ended") return req.refuse(refusal("member_ended"));
  req.batch.add(
    sent({
      mail_id: req.mailId,
      kind: "cancel",
      team: req.team.team_id,
      from: req.from,
      to: { name: row.name, generation: row.generation },
      provenance: req.provenance,
      causal: req.causal,
    }),
  );
  return req.done({
    member: refOf(req.team, row),
    status: "cancel_requested",
  } satisfies CancelRequested);
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
