import type { EventOf } from "../fold/state";
import type { KnownEvent, MailEnvelope } from "../log";
import {
  type CallContext,
  type Caller,
  callerOf,
  callMailId,
  callRequest,
  causalOf,
  named,
  type Refused,
  recorded,
  refusal,
} from "./call";
import { TEAM_CONSTANTS } from "./constants";
import { bodyOf, sent } from "./mail";
import { deliverable, type TeamLimits } from "./ops";
import { parkCall } from "./park";
import type { ReplyResult, Wire } from "./results";
import { askRow, type MemberRow } from "./rows";

// The model tools ask and reply (spec/schema/README.md, "Teams"; design §4.8 open and §4.9), each
// decided inside the caller's append. An ask stays a pending call until the asker's writer
// closes it (close.ts); a reply is mail to the asker. Reference: spec/tools/fixtures/ops_send.py.

/** What ask reads besides the store. */
export type AskPlan = {
  readonly limits: TeamLimits;
  /** Every budget covering the recipient has room for one request of its model. */
  readonly headroom: (row: MemberRow) => boolean;
  /** The ask's deadline from now, capped by the default (the model's ask takes none). */
  readonly timeoutMs?: number;
};

type Opened = {
  readonly status: "open";
  readonly ask_id: string;
  readonly deadline: number;
};

const eventsOf = (ctx: CallContext): readonly KnownEvent[] =>
  ctx.chain.events.flatMap((l) => (l.kind === "event" ? [l.event] : []));

/** The mail this log already sent under `mailId`: a re-dispatched call's own. */
function sentAs(ctx: CallContext, mailId: string): MailEnvelope | undefined {
  const e = eventsOf(ctx).find(
    (x): x is EventOf<"message_sent"> =>
      x.type === "message_sent" && x.data.envelope.mail_id === mailId,
  );
  return e?.data.envelope;
}

/**
 * ask.open: send with kind ask, a deadline and headroom on the recipient's budgets; the asker's
 * call stays pending and parks once its turn has nothing else to run. A re-dispatched ask only
 * parks. Returns the open ask, or the refusal it recorded.
 */
export function ask(
  ctx: CallContext,
  args: { readonly to: string; readonly question: string },
  plan: AskPlan,
): Opened | Refused {
  const caller = callerOf(ctx);
  if (caller === undefined) throw new Error("a team tool call outside a team");
  const askId = callMailId(ctx);
  const opened = sentAs(ctx, askId);
  if (opened !== undefined) return reopened(ctx, caller, opened);
  const row = deliverable(
    callRequest(ctx),
    "ask",
    named(ctx, args.to),
    plan.limits,
  );
  if ("refused" in row) return recorded(ctx, row);
  if (!plan.headroom(row)) return recorded(ctx, refusal("budget_exceeded"));
  const cap = TEAM_CONSTANTS.askWaitDefaultMs;
  const deadline = ctx.batch.now + Math.min(plan.timeoutMs ?? cap, cap);
  ctx.batch.add(
    sent({
      mail_id: askId,
      kind: "ask",
      team: caller.team.team_id,
      from: caller.ref,
      to: { name: row.name, generation: row.generation },
      provenance: caller.provenance,
      causal: causalOf(ctx),
      ask_id: askId,
      deadline,
      body: bodyOf(args.question, ctx.put),
    }),
  );
  parkCall(ctx, caller);
  return { status: "open", ask_id: askId, deadline } satisfies Opened;
}

function reopened(
  ctx: CallContext,
  caller: Caller,
  opened: MailEnvelope,
): Opened {
  if (opened.deadline === undefined)
    throw new Error("an ask envelope always has a deadline");
  parkCall(ctx, caller);
  return {
    status: "open",
    ask_id: callMailId(ctx),
    deadline: opened.deadline,
  };
}

/**
 * reply: the ask was delivered here, not replied to yet, and its row is open before its deadline,
 * read in this transaction. No policy decision: only an asked member replies.
 */
export function reply(
  ctx: CallContext,
  args: { readonly ask_id: string; readonly text: string },
): Wire<ReplyResult> | Refused {
  const caller = callerOf(ctx);
  if (caller === undefined) throw new Error("a team tool call outside a team");
  const events = eventsOf(ctx);
  const asked = events.find(
    (e): e is EventOf<"message_received"> =>
      e.type === "message_received" &&
      e.data.envelope.kind === "ask" &&
      e.data.envelope.ask_id === args.ask_id,
  )?.data.envelope;
  const row = askRow(ctx.db, args.ask_id);
  if (asked === undefined || row === undefined)
    return recorded(ctx, refusal("unknown_ask"));
  const replied = events.some(
    (e) =>
      e.type === "message_sent" &&
      e.data.envelope.kind === "reply" &&
      e.data.envelope.ask_id === args.ask_id,
  );
  if (replied) return recorded(ctx, refusal("already_replied"));
  if (row.state !== "open" || ctx.batch.now >= row.deadline)
    return recorded(ctx, refusal("ask_closed"));
  const id = callMailId(ctx);
  ctx.batch.add(
    sent({
      mail_id: id,
      kind: "reply",
      team: caller.team.team_id,
      from: caller.ref,
      to:
        "operator" in asked.from
          ? "team_log"
          : { name: asked.from.name, generation: asked.from.generation },
      provenance: asked.provenance,
      causal: causalOf(ctx),
      ask_id: args.ask_id,
      body: bodyOf(args.text, ctx.put),
    }),
  );
  return recorded(ctx, { id, status: "sent" } satisfies Wire<ReplyResult>);
}
