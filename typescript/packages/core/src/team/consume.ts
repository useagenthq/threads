import { assertNever } from "../assert-never";
import { loopParked } from "../fold/state";
import { type MailEnvelope, type Principal, principalKey } from "../log";
import { type CloseContext, takeAnswer, takeWaitNotice } from "./close";
import { received } from "./mail";
import { takeParkNotice } from "./park";
import { turnProvenance } from "./provenance";
import {
  type MemberRow,
  ownRows,
  pendingFor,
  pendingTo,
  teamOfLog,
  teamRow,
} from "./rows";
import { refuseAll } from "./settle";
import { parkedOn } from "./view";

// mail.consume under the recipient's writer (spec/schema/README.md, "Teams"; design §4.7): the
// pending rows in (created_at, mail_id) order, in one pass. Control mail is taken as it comes,
// whatever its provenance; ordinary mail only when the recipient was not parked when the consume
// began, as one batch of one (principal, root_request): mid-turn the open turn's, else the first
// row's, ending at the first row of another. Mail reaching a member that already ended is refused
// under its writer. Reference: spec/tools/fixtures/ops_consume.py.
//
// Applying a cancel is lane 21E.2's: until then a cancel stays pending, and it stops the ordinary
// mail behind it (a member being cancelled takes no new work).

export type ConsumeContext = CloseContext & {
  /** A member run's principal: ordinary mail of another waits for a run under that one. */
  readonly principal?: Principal;
};

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
 * Mail a consume can take now, so the worker wakes its recipient for it: all but a cancel, which
 * lane 21E.2 applies.
 */
export function consumable(env: MailEnvelope): boolean {
  return env.kind !== "cancel";
}

/**
 * Mail that may be control mail for a parked recipient (an answer, a notice, a park notice), so
 * the worker wakes a parked member for it. Its consume decides.
 */
export function mayResume(env: MailEnvelope): boolean {
  return (
    env.kind === "reply" ||
    env.kind === "member_settled" ||
    env.kind === "member_ended" ||
    env.kind === "member_parked" ||
    (env.kind === "bounce" && env.ask_id !== undefined)
  );
}

/** Consumes this writer's pending mail into the batch. */
export async function consume(ctx: ConsumeContext): Promise<Consumed> {
  if (ctx.chain.fold.team.teamLog) return consumeTeamLog(ctx);
  const rows = await ownRows(ctx.tx, ctx.threadId);
  const pending = await pendingFor(ctx.tx, rows);
  if (pending.length === 0) return { status: "nothing_pending" };
  const ended = rows.find((r) => r.state === "ended");
  if (ended !== undefined) return await refuseEnded(ctx, ended);
  const { fold } = ctx.chain;
  const open = fold.turnOpen
    ? await turnProvenance(ctx.tx, ctx.chain)
    : undefined;
  const pass: Pass = {
    turn: open === undefined ? undefined : pairOf(open),
    blocked: loopParked(fold).length > 0,
    batch: undefined,
  };
  // In order: each row's take reads what the rows before it took.
  const mailIds: string[] = [];
  for (const env of pending)
    if (await take(ctx, pass, env)) mailIds.push(env.mail_id);
  return { status: "consumed", mailIds };
}

/**
 * The team log takes every pending row and never parks or opens a turn (design §4.3): its asks'
 * answers and its waits' notices close them, and anything else (a park notice, a task
 * notification, a returned message) is recorded only.
 */
async function consumeTeamLog(ctx: ConsumeContext): Promise<Consumed> {
  const team = await teamOfLog(ctx.tx, ctx.branchId);
  const pending =
    team === undefined ? [] : await pendingTo(ctx.tx, team.team_id, null);
  if (pending.length === 0) return { status: "nothing_pending" };
  // In order: each row's take reads what the rows before it took.
  const mailIds: string[] = [];
  for (const env of pending) {
    if (ctx.batch.taken().has(env.mail_id)) {
      mailIds.push(env.mail_id);
      continue;
    }
    const control = await controlOf(ctx, env);
    if (control === "later") continue;
    if (control === "park") await takeParkNotice(ctx, env, true);
    else if (control === "ordinary") ctx.batch.add(received(env));
    mailIds.push(env.mail_id);
  }
  return { status: "consumed", mailIds };
}

/** Whether this pass takes the row: as control mail, or into its one ordinary batch. */
async function take(
  ctx: ConsumeContext,
  pass: Pass,
  env: MailEnvelope,
): Promise<boolean> {
  // An earlier control row of this pass took it (an ask's reply, a wait's notice).
  if (ctx.batch.taken().has(env.mail_id)) return true;
  const control = await controlOf(ctx, env);
  if (control === "later") {
    pass.blocked = true;
    return false;
  }
  if (control === "taken") return true;
  if (control === "park") {
    // Ordinary mail behind a new park waits for the next consume.
    if (await takeParkNotice(ctx, env, false)) pass.blocked = true;
    return true;
  }
  if (pass.blocked) return false;
  const pair = pairOf(env.provenance);
  if (pass.turn !== undefined) {
    if (pair !== pass.turn) return false;
  } else if (
    (pass.batch !== undefined && pair !== pass.batch) ||
    !underRun(ctx, env)
  ) {
    pass.blocked = true;
    return false;
  }
  pass.batch = pair;
  ctx.batch.add(received(env));
  return true;
}

function underRun(ctx: ConsumeContext, env: MailEnvelope): boolean {
  return (
    ctx.principal === undefined ||
    principalKey(ctx.principal) === principalKey(env.provenance.principal)
  );
}

/**
 * Control mail, the exhaustive list, applied now ("taken"; a park notice is "park"): a reply or
 * an ask's bounce, a wait's notice, and a task or end notice resolving a `{kind: member}` park. A
 * cancel waits for lane 21E.2 ("later"); anything else is ordinary.
 */
async function controlOf(
  ctx: ConsumeContext,
  env: MailEnvelope,
): Promise<"taken" | "park" | "later" | "ordinary"> {
  switch (env.kind) {
    case "member_parked":
      return "park";
    case "cancel":
      return "later";
    case "reply":
      await takeAnswer(ctx, env);
      return "taken";
    case "bounce":
      if (env.ask_id === undefined) return "ordinary";
      await takeAnswer(ctx, env);
      return "taken";
    case "member_settled":
    case "member_ended":
      return (await takeNotice(ctx, env)) ? "taken" : "ordinary";
    case "message":
    case "ask":
    case "task":
      return "ordinary";
    default:
      return assertNever(env.kind);
  }
}

/** A settle or end notice: a wait's, or a task or end notice resolving the park on it. */
async function takeNotice(
  ctx: ConsumeContext,
  env: MailEnvelope,
): Promise<boolean> {
  if (await takeWaitNotice(ctx, env)) return true;
  const address = { kind: "member", id: env.monitor_id ?? "" } as const;
  if (!parkedOn(ctx.chain, ctx.batch, address)) return false;
  const got = ctx.batch.add(received(env));
  ctx.batch.add({
    type: "resumed",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: { address, cause_event_id: got },
  });
  return true;
}

/** An ended member refuses what still reaches it; only a message or an ask is bounced. */
async function refuseEnded(
  ctx: ConsumeContext,
  row: MemberRow,
): Promise<Consumed> {
  const team = await teamRow(ctx.tx, row.team_id);
  const last = ctx.chain.events.findLast(
    (l) => l.kind === "event" && l.event.type === "member_ended",
  );
  if (
    team === undefined ||
    last?.kind !== "event" ||
    last.event.type !== "member_ended"
  )
    throw new Error("an ended member's log records its member_ended");
  const refusedMail = await refuseAll(
    ctx,
    team,
    row,
    last.event.data.result,
    false,
  );
  return { status: "refused", mailIds: refusedMail.map((m) => m.mail_id) };
}
