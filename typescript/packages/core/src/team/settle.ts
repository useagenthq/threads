import type { z } from "zod";
import type {
  MailEnvelope,
  Provenance,
  StoredMemberResult,
  ThreadId,
} from "../log";
import type { SqliteDriver } from "../store/driver";
import type { Batch } from "./batch";
import {
  addressOf,
  bodyOf,
  type Envelope,
  type PutText,
  refused,
  sent,
} from "./mail";
import {
  type MemberRow,
  memberRows,
  monitorsOn,
  ownRows,
  pendingTo,
  refOf,
  type TeamRow,
  teamRow,
} from "./rows";

// A member's own settling append (spec/schema/README.md, "Teams", Settling): with the
// turn_completed that ends its task, member_idle or member_ended, one notification per monitor
// row it fires (in monitor_id order), and for an end a mail_refused plus a bounce per pending
// inbound mail (in (created_at, mail_id) order) and, for a lead, one cancel per live member (in
// name order). The index hooks delete each fired monitor, return each refused row and close the
// team in the same transaction. Reference: spec/tools/fixtures/ops_life.py.

type Result = z.input<typeof StoredMemberResult>;
/** How a member's task ended: idle with its answer's text, or ended with its outcome. */
export type Settlement =
  | { readonly status: "completed"; readonly output: string }
  | DistributiveOmit<Exclude<Result, { status: "completed" }>, "member">;
type DistributiveOmit<T, K extends PropertyKey> = T extends unknown
  ? Omit<T, K>
  : never;

/** The writer a team append goes through, and its batch. */
export type AppendContext = {
  readonly db: SqliteDriver;
  readonly batch: Batch;
  readonly threadId: ThreadId;
  readonly branchId: string;
};

/** What a settling append reads besides: the turn it closes, and where big text goes. */
export type SettleContext = AppendContext & {
  /** The provenance of the turn the settlement closes: its notifications belong to it. */
  readonly provenance: Provenance;
  readonly put: PutText;
};

const HOST = {
  type_version: 1,
  critical: true,
  actor: { kind: "host" },
} as const;

const FIRES = {
  member_idle: new Set(["settle", "task"]),
  member_ended: new Set(["settle", "task", "end"]),
} as const;

/** Appends the settlement to the batch; a thread that is no team member settles nothing. */
export function settle(ctx: SettleContext, how: Settlement): void {
  const rows = ownRows(ctx.db, ctx.threadId);
  const primary = rows.find((r) => r.role === "member") ?? rows[0];
  if (primary === undefined) return;
  const team = teamRow(ctx.db, primary.team_id);
  if (team === undefined) throw new Error(`no teams row ${primary.team_id}`);
  const member = refOf(team, primary);
  const teamOf = (row: MemberRow): TeamRow =>
    teamRow(ctx.db, row.team_id) ?? team;
  if (how.status === "completed") {
    const output = bodyOf(how.output, ctx.put);
    const result: Result = { member, status: how.status, output };
    const settled = ctx.batch.add({
      ...HOST,
      type: "member_idle",
      data: { result },
    });
    for (const row of rows)
      fire(ctx, teamOf(row), row, "member_idle", settled, result);
    return;
  }
  const result: Result = { member, ...how };
  const settled = ctx.batch.add({
    ...HOST,
    type: "member_ended",
    data: { result },
  });
  for (const row of rows) {
    fire(ctx, teamOf(row), row, "member_ended", settled, result);
    refuseAll(ctx, teamOf(row), row, result, true);
    if (row.role === "lead") close(ctx, teamOf(row), row, settled);
  }
}

function mailId(ctx: AppendContext): string {
  return `${ctx.branchId}:${ctx.batch.nextId()}`;
}

function causal(ctx: AppendContext, eventId: string): Envelope["causal"] {
  return { thread_id: ctx.threadId, event_id: eventId };
}

/** One notification per monitor row on this generation that the event fires. */
function fire(
  ctx: SettleContext,
  team: TeamRow,
  row: MemberRow,
  type: keyof typeof FIRES,
  settled: string,
  result: Result,
): void {
  for (const m of monitorsOn(ctx.db, row.team_id, row)) {
    if (!FIRES[type].has(m.kind)) continue;
    ctx.batch.add(
      sent({
        mail_id: mailId(ctx),
        kind: type === "member_idle" ? "member_settled" : "member_ended",
        team: row.team_id,
        from: refOf(team, row),
        to: addressOf(ctx.db, row.team_id, m.watcher_branch_id),
        provenance: ctx.provenance,
        causal: causal(ctx, settled),
        monitor_id: m.monitor_id,
        result,
      }),
    );
  }
}

/**
 * Every pending inbound mail of an ended member is refused under its writer: a mail_refused and,
 * at its end append or for a message or an ask, a bounce to its sender carrying the refused
 * mail's provenance (an ask's bounce also carries the ended member's result).
 */
export function refuseAll(
  ctx: AppendContext,
  team: TeamRow,
  row: MemberRow,
  result: Result,
  bounceAll: boolean,
): readonly MailEnvelope[] {
  const taken = ctx.batch.taken();
  const pending = pendingTo(ctx.db, row.team_id, row.name).filter(
    (m) => !taken.has(m.mail_id),
  );
  for (const mail of pending) {
    const why = ctx.batch.add(refused(mail.mail_id));
    if (bounceAll || mail.kind === "message" || mail.kind === "ask")
      ctx.batch.add(sent(bounce(ctx, team, row, mail, why, result)));
  }
  return pending;
}

function bounce(
  ctx: AppendContext,
  team: TeamRow,
  row: MemberRow,
  mail: MailEnvelope,
  why: string,
  result: Result,
): Envelope {
  const back: Envelope["to"] =
    "operator" in mail.from
      ? "team_log"
      : { name: mail.from.name, generation: mail.from.generation };
  return {
    mail_id: mailId(ctx),
    kind: "bounce",
    team: row.team_id,
    from: refOf(team, row),
    to: back,
    provenance: mail.provenance,
    causal: causal(ctx, why),
    code: "member_ended",
    ...(mail.kind === "ask" && mail.ask_id !== undefined
      ? { ask_id: mail.ask_id, result }
      : {}),
  };
}

/** A lead's end closes its team: one cancel per live member, in name order. */
function close(
  ctx: SettleContext,
  team: TeamRow,
  lead: MemberRow,
  settled: string,
): void {
  // memberRows reads in name order.
  const live = memberRows(ctx.db, lead.team_id).filter(
    (r) => r.role === "member" && r.state !== "ended",
  );
  for (const r of live)
    ctx.batch.add(
      sent({
        mail_id: mailId(ctx),
        kind: "cancel",
        team: lead.team_id,
        from: refOf(team, lead),
        to: { name: r.name, generation: r.generation },
        provenance: ctx.provenance,
        causal: causal(ctx, settled),
      }),
    );
}
