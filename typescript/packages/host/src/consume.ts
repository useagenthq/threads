import {
  API_CHANNEL,
  assertNever,
  BranchId,
  cancel,
  cancelChildren,
  control,
  decide,
  type EventDraft,
  EventId,
  knownEvents,
  type Store,
  stopWhenIdle,
  storeConnection,
  type ThreadId,
  uuidv7,
  type Writer,
} from "@threads/core/host";
import { type Replied, replyToQuestion } from "./answers";
import { type HostContext, type HostedAgent, newPin, samePin } from "./context";
import {
  type Conversation,
  consumed,
  consumes,
  type InboxItem,
  pendingItems,
} from "./inbox";

// The run side of channel intake (step 6; spec/schema/README.md, "Channel
// replies"): items leave the inbox under the branch lease, a message as channel_delivery then
// user_input{source: channel}, a decision as an approval by a principal with approval
// authority, a control as a cancel or a stop_when_idle. An item is consumed in the transaction that applies it, so a
// busy branch leaves it queued; a decision or control never waits behind a message the branch
// can't take yet. While a question is open, the asker's message is its answer (answers.ts), and
// anyone else's waits without holding the asker's up.

type Message = InboxItem & { readonly item: { readonly kind: "message" } };

/** Consumes the thread's items until none can go now. */
export async function consume(
  ctx: HostContext,
  tenant: string,
  threadId: ThreadId,
): Promise<void> {
  const { db } = await storeConnection(ctx.store);
  for (;;) {
    const pending = await pendingItems(db, tenant, threadId);
    if (!pending.ok || !(await step(ctx, tenant, threadId, pending.value)))
      return;
  }
}

/**
 * Applies the first item that can go: messages in order until one is busy, then decisions and
 * controls in order until one is busy. True when one was consumed (the list is read again).
 */
async function step(
  ctx: HostContext,
  tenant: string,
  threadId: ThreadId,
  items: readonly InboxItem[],
): Promise<boolean> {
  let messagesWait = false;
  for (const next of items) {
    if (next.item.kind === "message" && messagesWait) continue;
    // An item whose channel or pinned agent this host lacks waits, like a busy one, for a host
    // that has them: every host sweeps every pending row, so discarding it here would lose it.
    const t = await target(ctx, tenant, threadId, next);
    const outcome = t === undefined ? "busy" : await item(ctx, t, next);
    if (outcome === "done") return true;
    if (outcome === "held") continue;
    if (next.item.kind !== "message") return false;
    messagesWait = true;
  }
  return false;
}

type Target = {
  readonly hosted: HostedAgent;
  readonly conversation: Conversation;
  readonly threadId: ThreadId;
};

/** The thread's agent: the one its log pins (a handoff target too), else the channel's. */
async function target(
  ctx: HostContext,
  tenant: string,
  threadId: ThreadId,
  next: InboxItem,
): Promise<Target | undefined> {
  // An api item is Thread.cancel's durable control (lane 29F): no channel adapter, its address
  // is the thread id, nothing is delivered, and its agent comes from the thread's own log.
  const api = next.channel === API_CHANNEL;
  if (api && next.item.kind !== "control")
    throw new Error("an api inbox item is a control item");
  const adapter = api ? undefined : ctx.channels.get(next.channel);
  if (!api && adapter === undefined) return undefined;
  const { log } = await ctx.open(tenant);
  const main = await log.mainBranch(threadId);
  const read = main.ok ? await log.read(main.value) : undefined;
  const events = read?.ok === true ? knownEvents(read.value) : [];
  // A root another host just made has no thread_started yet: it pins no agent, like no root.
  // Read as "no host agent", the item would be discarded and the message lost (F9.6 drill).
  const started = events.some((e) => e.type === "thread_started");
  const channelAgent =
    adapter === undefined ? undefined : ctx.agents.get(adapter.agent);
  const hosted = started ? ctx.agentOf(events) : channelAgent;
  // An agent of the pinned name but another config can't continue the thread: another host may.
  if (hosted === undefined || (started && !(await samePin(events, hosted))))
    return undefined;
  const conversation = {
    tenant,
    channel: next.channel,
    installation: next.installation_id,
    address: next.item.address,
  };
  return { hosted, conversation, threadId };
}

async function item(
  ctx: HostContext,
  t: Target,
  next: InboxItem,
): Promise<"busy" | "done" | "held"> {
  const { item } = next;
  switch (item.kind) {
    case "message":
      return message(ctx, t, { ...next, item });
    case "decision":
    case "control":
      return applied(ctx, t, next);
    default:
      return assertNever(item);
  }
}

async function message(
  ctx: HostContext,
  t: Target,
  next: Message,
): Promise<"busy" | "done" | "held"> {
  const { tenant } = t.conversation;
  const { log } = await ctx.open(tenant);
  const main = await log.mainBranch(t.threadId);
  let branchId = main.ok ? main.value : BranchId.parse(uuidv7(log.now()));
  if (!main.ok) {
    // Another process may have made the thread's root at the same moment: one root stands
    // (store.sql branches_root), and this process continues it whichever made it.
    await log.createBranch(t.threadId, branchId);
    const root = await log.mainBranch(t.threadId);
    if (!root.ok) return "busy";
    branchId = root.value;
  }
  const writer = await log.acquire(branchId, `host-${crypto.randomUUID()}`);
  if (!writer.ok) return "busy";
  let outcome: Replied | "input";
  try {
    const w = writer.value;
    // A turn in progress takes no new input, but the asker's reply answers its open question.
    outcome = w.chain.fold.turnOpen
      ? await replyToQuestion(w, log.now(), {
          inboxId: next.inbox_id,
          principal: next.item.principal,
          text: textOf(next),
          delivery: delivery(next),
        })
      : await appendInput(w, t, next, ctx.storeFor(tenant), log.now());
  } finally {
    await writer.value.release();
  }
  const thread = { id: t.threadId, branch: branchId };
  if (outcome === "busy" || outcome === "held") return outcome;
  // A rejected reply's correction is derived from the log like any reply (outbound.ts).
  if (outcome === "rejected") void ctx.replies(tenant, thread);
  // The run goes on alone, its replies after it in the same lane (outbound.ts): awaited, it
  // would hold this thread's consumer, and a cancel sent during the run would wait for its end.
  else void ctx.resume(t.hosted, tenant, next.item.principal, thread);
  return "done";
}

/** The message as the turn's input, after the thread's thread_started when it is new. */
async function appendInput(
  w: Writer,
  t: Target,
  next: Message,
  store: Store,
  now: number,
): Promise<"input" | "busy"> {
  // Only the thread's first event is its thread_started, whichever process creates it.
  const first: readonly EventDraft[] =
    w.chain.fold.seq === 0 ? [await newPin(t.hosted, store, true)] : [];
  // Checked again on the chain this lease holds: another host may have pinned another config
  // since target() looked, and nothing can be appended under it until release.
  if (first.length === 0 && !(await samePin(knownEvents(w.chain), t.hosted)))
    return "busy";
  // One append: the delivery names its own id so the input can cite it, and the item is
  // consumed at the input's seq in the same transaction.
  const cause = EventId.parse(uuidv7(now));
  const done = await w.append(
    [...first, { ...delivery(next), event_id: cause }, input(next, cause)],
    consumes(next.inbox_id),
  );
  return done.ok ? "input" : "busy";
}

function textOf(next: Message): string {
  const { content } = next.item;
  return typeof content === "string"
    ? content
    : content.flatMap((p) => (p.type === "text" ? [p.text] : [])).join("\n");
}

function delivery(next: Message): EventDraft {
  const { item } = next;
  const text = textOf(next);
  return {
    type: "channel_delivery",
    type_version: 1,
    critical: true,
    actor: { kind: "channel", principal: item.principal },
    data: {
      channel: next.channel,
      installation: next.installation_id,
      conversation: item.address,
      delivery_id: next.delivery_id,
      item_key: item.item_key,
      text,
    },
  };
}

function input(next: Message, cause: EventId): EventDraft {
  const { item } = next;
  return {
    type: "user_input",
    type_version: 1,
    critical: true,
    actor: { kind: "user", principal: item.principal },
    data: {
      source: "channel",
      delivery_event_id: cause,
      ...(typeof item.content === "string"
        ? { text: item.content }
        : { content: [...item.content] }),
    },
  };
}

/**
 * A decision from a principal with approval authority on the root run, or a control, appended
 * with the item's consumption in one transaction. branch_busy keeps the item queued; any other
 * refusal is final and consumes it with nothing appended.
 */
async function applied(
  ctx: HostContext,
  t: Target,
  next: InboxItem,
): Promise<"busy" | "done"> {
  const { tenant } = t.conversation;
  const { db } = await storeConnection(ctx.store);
  const { item } = next;
  const { log } = await ctx.open(tenant);
  const main = await log.mainBranch(t.threadId);
  const plan = await planOf(ctx, t, item);
  if (!main.ok || plan === undefined) {
    await db.transaction((tx) => consumed(tx, next.inbox_id, 0));
    return "done";
  }
  const alongside = consumes(next.inbox_id);
  const done = await control(log, main.value, item.principal, plan, {
    alongside,
  });
  if (!done.ok) {
    if (done.error.code === "branch_busy") return "busy";
    await db.transaction((tx) => consumed(tx, next.inbox_id, 0));
    return "done";
  }
  // Only a hard cancel reaches the tree; a soft stop lets running children finish.
  if (item.kind === "control" && item.command === "cancel")
    await cancelChildren(log, t.threadId, item.principal);
  void ctx.resume(t.hosted, tenant, item.principal, {
    id: t.threadId,
    branch: main.value,
  });
  return "done";
}

type PlanOf = Parameters<typeof control>[3];

async function planOf(
  ctx: HostContext,
  t: Target,
  item: InboxItem["item"],
): Promise<PlanOf | undefined> {
  const { log } = await ctx.open(t.conversation.tenant);
  switch (item.kind) {
    case "decision": {
      const may = await ctx.mayApprove(
        t.conversation.tenant,
        t.threadId,
        item.principal,
      );
      if (!may) return undefined;
      return decide(
        item.challenge_id,
        item.principal,
        log.now(),
        item.decision === "grant" ? { grant: true } : { grant: false },
      );
    }
    case "control":
      return item.command === "cancel"
        ? cancel(item.principal)
        : stopWhenIdle(item.principal);
    case "message":
      return undefined;
    default:
      return assertNever(item);
  }
}
