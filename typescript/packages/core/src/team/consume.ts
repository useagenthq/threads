import type { Fold } from "../fold/state";
import { type MailEnvelope, principalKey } from "../log";
import type { Chain } from "../verify";
import { received } from "./mail";
import { takeParkNotice } from "./park";
import { turnProvenance } from "./provenance";
import { type MemberRow, ownRows, pendingFor, teamRow } from "./rows";
import { type AppendContext, refuseAll } from "./settle";

// mail.consume under the recipient's writer (spec/schema/README.md, "Teams"; design §4.7): the
// pending rows in (created_at, mail_id) order. Control mail is taken whatever its provenance;
// ordinary mail only when the recipient was not parked when the consume began, as one batch of
// one (principal, root_request): mid-turn the open turn's, else the first row's, ending at the
// first row of another. Mail reaching a member that already ended is refused under its writer.
// Reference: spec/tools/fixtures/ops_consume.py.
//
// Lane 21E adds the rest of control mail: applying a cancel, and the replies, bounces and
// notices that complete an ask or a wait. Until then those stay pending, and a pending cancel
// stops the ordinary mail behind it: a member being cancelled takes no new work.

export type ConsumeContext = AppendContext & { readonly chain: Chain };

export type Consumed =
  | { readonly status: "nothing_pending" }
  | {
      readonly status: "consumed" | "refused";
      readonly mailIds: readonly string[];
    };

/** One turn's authority and budget root, as a key. */
function pairOf(provenance: MailEnvelope["provenance"]): string {
  const root = provenance.root_request;
  return `${principalKey(provenance.principal)} ${root.thread_id}:${root.event_id}`;
}

type Pass = {
  readonly turn: string | undefined;
  blocked: boolean;
  batch: string | undefined;
};

/**
 * Mail a consume can take now, so the worker wakes its recipient for it: all but the control mail
 * lane 21E consumes (a cancel, a reply, an ask's bounce).
 */
export function consumable(env: MailEnvelope): boolean {
  return !(
    env.kind === "cancel" ||
    env.kind === "reply" ||
    (env.kind === "bounce" && env.ask_id !== undefined)
  );
}

/** Consumes this writer's pending mail into the batch. */
export function consume(ctx: ConsumeContext): Consumed {
  const rows = ownRows(ctx.db, ctx.threadId);
  const pending = pendingFor(ctx.db, rows);
  if (pending.length === 0) return { status: "nothing_pending" };
  const ended = rows.find((r) => r.state === "ended");
  if (ended !== undefined) return refuseEnded(ctx, ended);
  const { fold } = ctx.chain;
  const open = fold.turnOpen ? turnProvenance(ctx.db, ctx.chain) : undefined;
  const pass: Pass = {
    turn: open === undefined ? undefined : pairOf(open),
    blocked: fold.parked.length > 0,
    batch: undefined,
  };
  const mailIds = pending.flatMap((env) =>
    take(ctx, fold, pass, env) ? [env.mail_id] : [],
  );
  return { status: "consumed", mailIds };
}

/** Whether this pass takes the row: as control mail, or into its one ordinary batch. */
function take(
  ctx: ConsumeContext,
  fold: Fold,
  pass: Pass,
  env: MailEnvelope,
): boolean {
  const control = controlOf(fold, env);
  if (control === "later") {
    if (env.kind === "cancel") pass.blocked = true;
    return false;
  }
  if (control === "resume") {
    resume(ctx, env);
    return true;
  }
  if (control === "park") {
    // Ordinary mail behind a new park waits for the next consume.
    if (takeParkNotice(ctx, env, false)) pass.blocked = true;
    return true;
  }
  if (pass.blocked) return false;
  const pair = pairOf(env.provenance);
  if (pass.turn !== undefined) {
    if (pair !== pass.turn) return false;
  } else if (pass.batch !== undefined && pair !== pass.batch) {
    pass.blocked = true;
    return false;
  }
  pass.batch = pair;
  ctx.batch.add(received(env));
  return true;
}

/** A task or end notice resolving the park on it: its receipt, then resumed. */
function resume(ctx: ConsumeContext, env: MailEnvelope): void {
  const got = ctx.batch.add(received(env));
  ctx.batch.add({
    type: "resumed",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: {
      address: { kind: "member", id: env.monitor_id ?? "" },
      cause_event_id: got,
    },
  });
}

/**
 * Control mail this lane takes: a member's park notice, and a task or end notice resolving a
 * `{kind: member}` park. The rest of control mail waits for lane 21E ("later"); anything else is
 * ordinary.
 */
function controlOf(
  fold: Fold,
  env: MailEnvelope,
): "resume" | "park" | "later" | "ordinary" {
  switch (env.kind) {
    case "member_parked":
      return "park";
    case "cancel":
    case "reply":
      return "later";
    case "bounce":
      return env.ask_id === undefined ? "ordinary" : "later";
    case "member_settled":
    case "member_ended": {
      const monitor = env.monitor_id;
      if (monitor !== undefined && fold.team.settle.has(monitor))
        return "later";
      const parked = fold.parked.some(
        (p) => p.kind === "member" && p.id === monitor,
      );
      return parked ? "resume" : "ordinary";
    }
    default:
      return "ordinary";
  }
}

/** An ended member refuses what still reaches it; only a message or an ask is bounced. */
function refuseEnded(ctx: ConsumeContext, row: MemberRow): Consumed {
  const team = teamRow(ctx.db, row.team_id);
  const last = ctx.chain.events.findLast(
    (l) => l.kind === "event" && l.event.type === "member_ended",
  );
  if (
    team === undefined ||
    last?.kind !== "event" ||
    last.event.type !== "member_ended"
  )
    throw new Error("an ended member's log records its member_ended");
  const refusedMail = refuseAll(ctx, team, row, last.event.data.result, false);
  return { status: "refused", mailIds: refusedMail.map((m) => m.mail_id) };
}
