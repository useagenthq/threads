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
import type { Request, Target } from "./request";
import type { ReplyResult, Wire } from "./results";
import { askRow, type MemberRow } from "./rows";

// The model tools ask and reply (spec/schema/README.md, "Teams"; design §4.8 open and §4.9), each
// decided inside the caller's append. An ask stays a pending call until the asker's writer
// closes it (close.ts); a reply is mail to the asker. Reference: spec/tools/fixtures/ops_send.py.

/** What ask reads besides the store. */
export type AskPlan = {
  readonly limits: TeamLimits;
  /** Every budget covering the recipient has room for one request of its model. */
  readonly headroom: (row: MemberRow) => Promise<boolean>;
  /** The ask's deadline from now, capped by the default (the model's ask takes none). */
  readonly timeoutMs?: number;
};

/** An ask sent: its id and deadline. */
export type AskOpened = {
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
export async function ask(
  ctx: CallContext,
  args: { readonly to: string; readonly question: string },
  plan: AskPlan,
): Promise<AskOpened | Refused> {
  const caller = await callerOf(ctx);
  if (caller === undefined) throw new Error("a team tool call outside a team");
  const opened = sentAs(ctx, callMailId(ctx));
  if (opened !== undefined) return reopened(ctx, caller, opened);
  const got = await openAsk(
    await callRequest(ctx),
    named(ctx, args.to),
    args.question,
    plan,
  );
  if (got.status === "open") await parkCall(ctx, caller);
  return got;
}

/**
 * The ask's mail, for a model call or an operator request: the checks send makes, headroom, then
 * message_sent{ask} with its deadline. Nothing records the open ask as a result.
 */
export async function openAsk(
  req: Request,
  to: Target,
  question: string,
  plan: AskPlan,
): Promise<AskOpened | Refused> {
  const row = await deliverable(req, "ask", to, plan.limits);
  if ("refused" in row) return req.refuse(row);
  if (!(await plan.headroom(row)))
    return req.refuse(refusal("budget_exceeded"));
  const cap = TEAM_CONSTANTS.askWaitDefaultMs;
  const deadline = req.batch.now + Math.min(plan.timeoutMs ?? cap, cap);
  req.batch.add(
    sent({
      mail_id: req.mailId,
      kind: "ask",
      team: req.team.team_id,
      from: req.from,
      to: { name: row.name, generation: row.generation },
      provenance: req.provenance,
      causal: req.causal,
      ask_id: req.mailId,
      deadline,
      body: await bodyOf(question, req.put),
    }),
  );
  return { status: "open", ask_id: req.mailId, deadline };
}

async function reopened(
  ctx: CallContext,
  caller: Caller,
  opened: MailEnvelope,
): Promise<AskOpened> {
  if (opened.deadline === undefined)
    throw new Error("an ask envelope always has a deadline");
  await parkCall(ctx, caller);
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
export async function reply(
  ctx: CallContext,
  args: { readonly ask_id: string; readonly text: string },
): Promise<Wire<ReplyResult> | Refused> {
  const caller = await callerOf(ctx);
  if (caller === undefined) throw new Error("a team tool call outside a team");
  const events = eventsOf(ctx);
  const asked = events.find(
    (e): e is EventOf<"message_received"> =>
      e.type === "message_received" &&
      e.data.envelope.kind === "ask" &&
      e.data.envelope.ask_id === args.ask_id,
  )?.data.envelope;
  const row = await askRow(ctx.tx, args.ask_id);
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
          : "caller" in asked.from
            ? { caller: asked.from.caller }
            : { name: asked.from.name, generation: asked.from.generation },
      provenance: asked.provenance,
      causal: causalOf(ctx),
      ask_id: args.ask_id,
      body: await bodyOf(args.text, ctx.put),
    }),
  );
  return recorded(ctx, { id, status: "sent" } satisfies Wire<ReplyResult>);
}
