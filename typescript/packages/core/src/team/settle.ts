import type { z } from "zod";
import type {
  MailEnvelope,
  Provenance,
  StoredMemberResult,
  ThreadId,
} from "../log";
import type { Tx } from "../store/driver";
import type { Batch } from "./batch";
import {
  addressOf,
  bodyOf,
  type Envelope,
  type PutText,
  refused,
  replyAddress,
  sent,
} from "./mail";
import {
  type MemberRow,
  mailEnvelope,
  memberRows,
  monitorsOn,
  openAsks,
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
  readonly tx: Tx;
  readonly batch: Batch;
  readonly threadId: ThreadId;
  readonly branchId: string;
};

/** What a settling append reads besides: the turn it closes, and where big text goes. */
export type SettleContext = AppendContext & {
  /**
   * The provenance of the turn the settlement closes: its notifications belong to it. Undefined
   * only where there is no turn — a host member whose rebind failed at materialize, which has no
   * monitor to notify either (Phase 2).
   */
  readonly provenance: Provenance | undefined;
  readonly put: PutText;
  /**
   * A host member's asks its last turn took and never answered (coordinator decision 6,
   * 2026-09-26): its end bounces each one member_ended, since the end's refusal of pending mail
   * cannot reach a row that is already consumed.
   */
  readonly takenAsks?: readonly string[];
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
export async function settle(
  ctx: SettleContext,
  how: Settlement,
): Promise<void> {
  const rows = await ownRows(ctx.tx, ctx.threadId);
  const primary = rows.find((r) => r.role === "member") ?? rows[0];
  if (primary === undefined) return;
  const team = await teamRow(ctx.tx, primary.team_id);
  if (team === undefined) throw new Error(`no teams row ${primary.team_id}`);
  const member = refOf(team, primary);
  const teamOf = async (row: MemberRow): Promise<TeamRow> =>
    (await teamRow(ctx.tx, row.team_id)) ?? team;
  if (how.status === "completed") {
    const output = await bodyOf(how.output, ctx.put);
    const result: Result = { member, status: how.status, output };
    const settled = ctx.batch.add({
      ...HOST,
      type: "member_idle",
      data: { result },
    });
    for (const row of rows)
      await fire(ctx, await teamOf(row), row, "member_idle", settled, result);
    return;
  }
  const result: Result = { member, ...how };
  // The member's own open asks never outlive it: each closes cancelled before its end.
  const closed = new Set(
    ctx.batch.drafts.flatMap((d) =>
      d.type === "ask_closed" ? [d.data.ask_id] : [],
    ),
  );
  for (const askId of await openAsks(ctx.tx, ctx.branchId))
    if (!closed.has(askId))
      ctx.batch.add({
        ...HOST,
        type: "ask_closed",
        data: { ask_id: askId, outcome: { status: "cancelled" } },
      });
  const settled = ctx.batch.add({
    ...HOST,
    type: "member_ended",
    data: { result },
  });
  for (const row of rows) {
    await fire(ctx, await teamOf(row), row, "member_ended", settled, result);
    await refuseAll(ctx, await teamOf(row), row, result, true);
    await bounceTaken(ctx, await teamOf(row), row, result, settled);
    if (row.role === "lead") await close(ctx, await teamOf(row), row, settled);
  }
}

/**
 * A host member's end bounces every ask it consumed and never answered (coordinator decision 6):
 * those rows are no longer pending, so the end's refusal cannot reach them, and without this they
 * would wait out their deadlines. Reference: spec/tools/fixtures/ref_host.py.
 */
async function bounceTaken(
  ctx: SettleContext,
  team: TeamRow,
  row: MemberRow,
  result: Result,
  settled: string,
): Promise<void> {
  if (row.role !== "host_member") return;
  for (const askId of ctx.takenAsks ?? []) {
    const ask = await mailEnvelope(ctx.tx, askId);
    if (ask === undefined) continue;
    ctx.batch.add(
      sent({
        mail_id: mailId(ctx),
        kind: "bounce",
        team: row.team_id,
        from: refOf(team, row),
        to: replyAddress(ask),
        provenance: ask.provenance,
        causal: causal(ctx, settled),
        code: "member_ended",
        ask_id: askId,
        result,
      }),
    );
  }
}

function mailId(ctx: AppendContext): string {
  return `${ctx.branchId}:${ctx.batch.nextId()}`;
}

function causal(ctx: AppendContext, eventId: string): Envelope["causal"] {
  return { thread_id: ctx.threadId, event_id: eventId };
}

/** One notification per monitor row on this generation that the event fires. */
async function fire(
  ctx: SettleContext,
  team: TeamRow,
  row: MemberRow,
  type: keyof typeof FIRES,
  settled: string,
  result: Result,
): Promise<void> {
  for (const m of await monitorsOn(ctx.tx, row.team_id, row)) {
    if (!FIRES[type].has(m.kind)) continue;
    if (ctx.provenance === undefined)
      throw new Error("a fired monitor belongs to a turn");
    ctx.batch.add(
      sent({
        mail_id: mailId(ctx),
        kind: type === "member_idle" ? "member_settled" : "member_ended",
        team: row.team_id,
        from: refOf(team, row),
        to: await addressOf(ctx.tx, row.team_id, m.watcher_branch_id),
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
export async function refuseAll(
  ctx: AppendContext,
  team: TeamRow,
  row: MemberRow,
  result: Result,
  bounceAll: boolean,
): Promise<readonly MailEnvelope[]> {
  const taken = ctx.batch.taken();
  const pending = (await pendingTo(ctx.tx, row.team_id, row.name)).filter(
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
  return {
    mail_id: mailId(ctx),
    kind: "bounce",
    team: row.team_id,
    from: refOf(team, row),
    to: replyAddress(mail),
    provenance: mail.provenance,
    causal: causal(ctx, why),
    code: "member_ended",
    ...(mail.kind === "ask" && mail.ask_id !== undefined
      ? { ask_id: mail.ask_id, result }
      : {}),
  };
}

/** A lead's end closes its team: one cancel per live member, in name order. */
async function close(
  ctx: SettleContext,
  team: TeamRow,
  lead: MemberRow,
  settled: string,
): Promise<void> {
  // memberRows reads in name order.
  const live = (await memberRows(ctx.tx, lead.team_id)).filter(
    (r) => r.role === "member" && r.state !== "ended",
  );
  if (live.length === 0) return;
  const { provenance } = ctx;
  // A lead always ends inside a turn: only a settlement with no turn has no provenance.
  if (provenance === undefined) throw new Error("a lead's end closes a turn");
  for (const r of live)
    ctx.batch.add(
      sent({
        mail_id: mailId(ctx),
        kind: "cancel",
        team: lead.team_id,
        from: refOf(team, lead),
        to: { name: r.name, generation: r.generation },
        provenance,
        causal: causal(ctx, settled),
      }),
    );
}
