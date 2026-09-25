import type { ChannelAdapter } from "@threads/core";
import {
  type BranchId,
  correctionText,
  type KnownEvent,
  keepLease,
  knownEvents,
  questionText,
  storeConnection,
  ThreadId,
  type VerifiedLog,
} from "@threads/core/host";
import type { HostContext } from "./context";
import { sendOp } from "./deliver";
import { type Conversation, conversationOf } from "./inbox";
import { leftovers, settleLeftover } from "./leftovers";

// A channel thread's replies are derived from its log, whatever ran it (spec/schema/README.md,
// "Channel replies"): each end_turn reply and each open approval card is a host-issued
// channel_send with call_id send_<source seq>_<op index>; an open ask_user question is
// question_<call_id>_<op index>, and the correction of a rejected answer
// question_retry_<answer_rejected event_id>_<op index>. Every call the log
// lacks is issued, a begun one is reconciled by sendOp, and one with a result or a parked
// effect is left alone, so a crash between a turn's end and its reply loses nothing and never
// sends twice.

type Fold = VerifiedLog["fold"];
type Op = Parameters<ChannelAdapter["perform"]>[0];
type Call = {
  readonly callId: string;
  readonly op: Op;
  readonly requestId: string;
};

/** Issues the replies the thread's log lacks: "busy" when its lease is held elsewhere. */
export async function reply(
  ctx: HostContext,
  tenant: string,
  threadId: ThreadId,
): Promise<"done" | "busy"> {
  const { db } = await storeConnection(ctx.store);
  const conversation = await conversationOf(db, tenant, threadId);
  const adapter =
    conversation === undefined
      ? undefined
      : ctx.channels.get(conversation.channel);
  if (conversation === undefined || adapter === undefined) return "done";
  const { log, artifacts } = await ctx.open(tenant);
  const main = await log.mainBranch(threadId);
  const read = main.ok ? await log.read(main.value) : undefined;
  if (!main.ok || read?.ok !== true) return "done";
  const derived = calls(
    adapter,
    conversation,
    knownEvents(read.value),
    read.value.fold,
  );
  const due = new Set(derived.map((c) => c.callId));
  const todo = missing(main.value, read.value.fold, derived);
  const left = leftovers(
    main.value,
    read.value.fold,
    knownEvents(read.value),
    due,
  );
  if (todo.length > 0 || left.length > 0) {
    const writer = await log.acquire(main.value, `send-${crypto.randomUUID()}`);
    if (!writer.ok) return "busy";
    // Renewed while the sends are in flight: one past the fence may take longer than the TTL,
    // and a lapsed lease would let another host look it up as not sent and send it again.
    const release = keepLease(writer.value);
    try {
      const w = writer.value;
      for (const call of missing(main.value, w.chain.fold, todo))
        await sendOp(adapter, w, artifacts, call, ctx.stopping);
      const events = knownEvents(w.chain);
      for (const l of leftovers(main.value, w.chain.fold, events, due))
        await settleLeftover(adapter, w, artifacts, l, ctx.stopping);
    } finally {
      await release();
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
  // A CAS on the old route: moving it again after an unknown commit changes nothing.
  const routed = await db.transaction(async (tx) => {
    const rows = await tx.all(
      `UPDATE channel_threads SET thread_id = ? WHERE tenant_id = ? AND thread_id = ?
        RETURNING thread_id`,
      [to, tenant, threadId],
    );
    if (rows.length > 0)
      await tx.run(
        `UPDATE inbox SET thread_id = ? WHERE tenant_id = ? AND thread_id = ?
          AND consumed_seq IS NULL`,
        [to, tenant, threadId],
      );
    return rows.length > 0;
  });
  if (!routed && (await conversationOf(db, tenant, to)) === undefined)
    return "done";
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
  return sources(adapter, events, fold).flatMap(({ event, key, ops }) => {
    const before = events.slice(0, events.indexOf(event));
    const request = before.findLast((e) => e.type === "model_request");
    if (request === undefined) return [];
    const inbound =
      before.findLast((e) => e.type === "channel_delivery")?.time ?? event.time;
    return ops.map((op, i) => ({
      callId: `${key}_${i}`,
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

type Source = {
  readonly event: KnownEvent;
  /** The call_id prefix of its ops: send_<seq>, question_<call_id> or question_retry_<event_id>. */
  readonly key: string;
  readonly ops: readonly Op[];
};

/**
 * Each end_turn turn's last response, each open approval card, each open question and each
 * correction of a rejected answer to a question still open, in log order.
 */
function sources(
  adapter: ChannelAdapter,
  events: readonly KnownEvent[],
  fold: Fold,
): readonly Source[] {
  const out: Source[] = [];
  const rendered = (e: KnownEvent): Source => ({
    event: e,
    key: `send_${e.seq}`,
    ops: adapter.render(e),
  });
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
      out.push(rendered(response));
    if (e.type === "approval_requested" && open(fold, e.data))
      out.push(rendered(e));
    out.push(...question(adapter, e, fold));
  }
  return out;
}

/** An open question's message, or the correction after a reply that matched none of its options. */
function question(
  adapter: ChannelAdapter,
  e: KnownEvent,
  fold: Fold,
): readonly Source[] {
  const callId =
    e.type === "parked" && e.data.address.kind === "input"
      ? e.data.address.id
      : e.type === "answer_rejected"
        ? e.data.call_id
        : undefined;
  const ask = callId === undefined ? undefined : fold.asks.get(callId);
  const asking = fold.parked.some((a) => a.kind === "input" && a.id === callId);
  if (ask === undefined || ask === "invalid" || !asking) return [];
  return e.type === "answer_rejected"
    ? [
        {
          event: e,
          key: `question_retry_${e.event_id}`,
          ops: adapter.renderText(correctionText(ask)),
        },
      ]
    : [
        {
          event: e,
          key: `question_${callId}`,
          ops: adapter.renderText(questionText(ask)),
        },
      ];
}

function open(
  fold: Fold,
  asked: { readonly challenge_id: string; readonly expires_at: number },
): boolean {
  const state = fold.approvals.get(asked.challenge_id);
  return state?.consumed === false && asked.expires_at > Date.now();
}
