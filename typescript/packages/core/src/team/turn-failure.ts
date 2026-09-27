import type { EventOf } from "../fold/state";
import type { MailEnvelope, TurnFailure } from "../log";
import type { Chain } from "../verify";
import { type Envelope, replyAddress, sent } from "./mail";
import { mailEnvelope, ownRows, refOf, teamRow } from "./rows";
import type { AppendContext } from "./settle";

// A host member's turn-only failure (spec/schema/README.md, "Teams Phase 2", semantic rule 53): a
// hop cap, the caller's run budget, a model error or a turn-failing tool ends only that turn. Its
// ending append is, in order: optionally budget_exceeded{scope: hop}, turn_completed, one
// message_sent{kind: bounce, code: turn_failed, ask_id, error} per ask the turn took and left
// unanswered, then member_idle{turn_failed}. The row goes back to idle and keeps its last
// completed result. Reference: spec/tools/fixtures/ops_host.py.

const HOST = {
  type_version: 1,
  critical: true,
  actor: { kind: "host" },
} as const;

/** What the failing turn's ending append reads: its writer, and the log's open asks. */
export type TurnFailureContext = AppendContext & {
  /** The committed chain: its fold names the asks the open turn took and left unanswered. */
  readonly chain: Chain;
};

export type TurnFailureEnd = {
  readonly reason: EventOf<"turn_completed">["data"]["reason"];
  readonly code?: EventOf<"turn_completed">["data"]["code"];
};

/**
 * Appends the failing turn's end. `hop` is the hop cap's `budget_exceeded` data when the rule's
 * cap refused the turn. Returns the ask ids it bounced, which close `failed` at their askers.
 */
export async function turnFailure(
  ctx: TurnFailureContext,
  end: TurnFailureEnd,
  error: TurnFailure,
  hop?: EventOf<"budget_exceeded">["data"],
): Promise<{ readonly status: "idle"; readonly failed: readonly string[] }> {
  const rows = await ownRows(ctx.tx, ctx.threadId);
  const row = rows[0];
  if (row === undefined || row.role !== "host_member")
    throw new Error("a turn-only failure is a host member's");
  const team = await teamRow(ctx.tx, row.team_id);
  if (team === undefined) throw new Error(`no teams row ${row.team_id}`);
  if (hop !== undefined)
    ctx.batch.add({ ...HOST, type: "budget_exceeded", data: hop });
  const ended = ctx.batch.add({
    ...HOST,
    type: "turn_completed",
    data: {
      reason: end.reason,
      ...(end.code === undefined ? {} : { code: end.code }),
    },
  });
  // The asks this turn took and left unanswered, in the order the log received them.
  const failed = [...ctx.chain.fold.team.host.turnAsks];
  for (const askId of failed) {
    // An AskId is its ask's MailId, so the ask's own envelope carries its provenance and sender.
    const ask = await mailEnvelope(ctx.tx, askId);
    if (ask === undefined) throw new Error(`no mail row for ask ${askId}`);
    ctx.batch.add(sent(bounce(ctx, team, row, ask, ended, error)));
  }
  ctx.batch.add({ ...HOST, type: "member_idle", data: { turn_failed: error } });
  return { status: "idle", failed };
}

function bounce(
  ctx: TurnFailureContext,
  team: Parameters<typeof refOf>[0],
  row: Parameters<typeof refOf>[1],
  ask: MailEnvelope,
  ended: string,
  error: TurnFailure,
): Envelope {
  return {
    mail_id: `${ctx.branchId}:${ctx.batch.nextId()}`,
    kind: "bounce",
    team: row.team_id,
    from: refOf(team, row),
    to: replyAddress(ask),
    // A bounce belongs to the request that sent the refused mail (rule 43), never to this turn's.
    provenance: ask.provenance,
    causal: { thread_id: ctx.threadId, event_id: ended },
    ask_id: askId(ask),
    code: "turn_failed",
    error,
  };
}

function askId(ask: MailEnvelope): string {
  if (ask.ask_id === undefined) throw new Error("an ask carries its ask_id");
  return ask.ask_id;
}
