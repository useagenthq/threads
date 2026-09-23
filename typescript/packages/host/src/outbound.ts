import type { ChannelAdapter } from "@threads/core";
import {
  type BranchId,
  type KnownEvent,
  knownEvents,
  storeConnection,
  ThreadId,
  type VerifiedLog,
} from "@threads/core/host";
import type { HostContext } from "./context";
import { sendOp } from "./deliver";
import { type Conversation, conversationOf } from "./inbox";

// A channel thread's replies are derived from its log, whatever ran it (spec/schema/README.md, // "Channel replies"): each end_turn reply and each open approval card
// is a host-issued channel_send with call_id send_<source seq>_<op index>. Every call the log
// lacks is issued, a begun one is reconciled by sendOp, and one with a result or a parked
// effect is left alone, so a crash between a turn's end and its reply loses nothing and never
// sends twice.

type Fold = VerifiedLog["fold"];
type Call = {
  readonly callId: string;
  readonly op: Parameters<ChannelAdapter["perform"]>[0];
  readonly requestId: string;
};

/** Issues the replies the thread's log lacks: "busy" when its lease is held elsewhere. */
export async function reply(
  ctx: HostContext,
  tenant: string,
  threadId: ThreadId,
): Promise<"done" | "busy"> {
  const { db } = await storeConnection(ctx.store);
  const conversation = conversationOf(db, tenant, threadId);
  const adapter =
    conversation === undefined
      ? undefined
      : ctx.channels.get(conversation.channel);
  if (conversation === undefined || adapter === undefined) return "done";
  const { log, artifacts } = await ctx.open(tenant);
  const main = log.mainBranch(threadId);
  const read = main.ok ? log.read(main.value) : undefined;
  if (!main.ok || read?.ok !== true) return "done";
  const todo = missing(
    main.value,
    read.value.fold,
    calls(adapter, conversation, knownEvents(read.value), read.value.fold),
  );
  if (todo.length > 0) {
    const writer = log.acquire(main.value, `send-${crypto.randomUUID()}`);
    if (!writer.ok) return "busy";
    try {
      for (const call of missing(main.value, writer.value.chain.fold, todo))
        await sendOp(adapter, writer.value, artifacts, call);
    } finally {
      writer.value.release();
    }
  }
  return handOver(ctx, tenant, threadId, read.value);
}

/**
 * After a handoff the conversation routes to the target thread, whose own
 * answer is then the conversation's next reply.
 */
async function handOver(
  ctx: HostContext,
  tenant: string,
  threadId: ThreadId,
  read: VerifiedLog,
): Promise<"done" | "busy"> {
  const moved = knownEvents(read).findLast((e) => e.type === "handoff");
  if (moved?.type !== "handoff") return "done";
  const to = ThreadId.parse(moved.data.to_thread_id);
  const { db } = await storeConnection(ctx.store);
  // The route and the items queued behind it move together; 0 rows means another process
  // moved the route first, so it is re-read, never overwritten.
  const routed = db.transaction(() => {
    const rows = db.all(
      `UPDATE channel_threads SET thread_id = ? WHERE tenant_id = ? AND thread_id = ?
        RETURNING thread_id`,
      [to, tenant, threadId],
    );
    if (rows.length > 0)
      db.run(
        `UPDATE inbox SET thread_id = ? WHERE tenant_id = ? AND thread_id = ?
          AND consumed_seq IS NULL`,
        [to, tenant, threadId],
      );
    return rows.length > 0;
  });
  if (!routed && conversationOf(db, tenant, to) === undefined) return "done";
  return reply(ctx, tenant, to);
}

/** The derived calls with no result whose effect isn't parked. */
function missing(
  branch: BranchId,
  fold: Fold,
  all: readonly Call[],
): readonly Call[] {
  return all.filter(
    (c) =>
      fold.calls.get(c.callId)?.result === undefined &&
      !fold.parked.some(
        (a) => a.kind === "effect" && a.id === `${branch}:${c.callId}`,
      ),
  );
}

// ponytail: re-derives every past reply on each run (O(turns)); keep a replied-through seq per
// channel thread if long conversations make this measurable.
function calls(
  adapter: ChannelAdapter,
  conversation: Conversation,
  events: readonly KnownEvent[],
  fold: Fold,
): readonly Call[] {
  return sources(events, fold).flatMap((source) => {
    const before = events.slice(0, events.indexOf(source));
    const request = before.findLast((e) => e.type === "model_request");
    if (request === undefined) return [];
    const inbound =
      before.findLast((e) => e.type === "channel_delivery")?.time ??
      source.time;
    return adapter.render(source).map((op, i) => ({
      callId: `send_${source.seq}_${i}`,
      op: {
        ...op,
        address: conversation.address,
        installation_id: conversation.installation,
        last_inbound_at: inbound,
      },
      requestId: request.event_id,
    }));
  });
}

/** Each end_turn turn's last response and each open approval card, in log order. */
function sources(
  events: readonly KnownEvent[],
  fold: Fold,
): readonly KnownEvent[] {
  const out: KnownEvent[] = [];
  let response: KnownEvent | undefined;
  for (const e of events) {
    if (e.type === "user_input") response = undefined;
    if (
      (e.type === "model_response" || e.type === "model_response_recovered") &&
      fold.requests.get(e.data.request_event_id)?.compaction !== true
    )
      response = e;
    if (
      e.type === "turn_completed" &&
      e.data.reason === "end_turn" &&
      e.data.code === undefined &&
      response !== undefined
    )
      out.push(response);
    if (e.type === "approval_requested" && open(fold, e.data)) out.push(e);
  }
  return out;
}

function open(
  fold: Fold,
  asked: { readonly challenge_id: string; readonly expires_at: number },
): boolean {
  const state = fold.approvals.get(asked.challenge_id);
  return state?.consumed === false && asked.expires_at > Date.now();
}
