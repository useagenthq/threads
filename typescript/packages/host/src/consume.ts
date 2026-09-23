import type { ChannelAdapter } from "@threads/core";
import {
  assertNever,
  BranchId,
  cancel,
  cancelChildren,
  control,
  decide,
  type EventDraft,
  type EventId,
  knownEvents,
  stopWhenIdle,
  storeConnection,
  type ThreadId,
  uuidv7,
} from "@threads/core/host";
import { type HostContext, type HostedAgent, samePin } from "./context";
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
// can't take yet.

type Message = InboxItem & { readonly item: { readonly kind: "message" } };

/** Consumes the thread's items until none can go now. */
export async function consume(
  ctx: HostContext,
  tenant: string,
  threadId: ThreadId,
): Promise<void> {
  const { db } = await storeConnection(ctx.store);
  for (;;) {
    const pending = pendingItems(db, tenant, threadId);
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
    if (t !== undefined && (await item(ctx, t, next)) === "done") return true;
    if (next.item.kind !== "message") return false;
    messagesWait = true;
  }
  return false;
}

type Target = {
  readonly adapter: ChannelAdapter;
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
  const adapter = ctx.channels.get(next.channel);
  if (adapter === undefined) return undefined;
  const { log } = await ctx.open(tenant);
  const main = log.mainBranch(threadId);
  const read = main.ok ? log.read(main.value) : undefined;
  const events = read?.ok === true ? knownEvents(read.value) : [];
  // A root another host just made has no thread_started yet: it pins no agent, like no root.
  // Read as "no host agent", the item would be discarded and the message lost (F9.6 drill).
  const started = events.some((e) => e.type === "thread_started");
  const hosted = started ? ctx.agentOf(events) : ctx.agents.get(adapter.agent);
  // An agent of the pinned name but another config can't continue the thread: another host may.
  if (hosted === undefined || (started && !(await samePin(events, hosted))))
    return undefined;
  const conversation = {
    tenant,
    channel: next.channel,
    installation: next.installation_id,
    address: next.item.address,
  };
  return { adapter, hosted, conversation, threadId };
}

async function item(
  ctx: HostContext,
  t: Target,
  next: InboxItem,
): Promise<"busy" | "done"> {
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
): Promise<"busy" | "done"> {
  const { tenant } = t.conversation;
  const { log } = await ctx.open(tenant);
  const { db } = await storeConnection(ctx.store);
  const main = log.mainBranch(t.threadId);
  let branchId = main.ok ? main.value : BranchId.parse(uuidv7(log.now()));
  if (!main.ok) {
    const made = log.createBranch(t.threadId, branchId);
    // Another process may have made the thread's root at the same moment: the first is the one.
    const root = log.mainBranch(t.threadId);
    if (!made.ok || !root.ok) return "busy";
    branchId = root.value;
  }
  const writer = log.acquire(branchId, `host-${crypto.randomUUID()}`);
  if (!writer.ok) return "busy";
  try {
    if (writer.value.chain.fold.turnOpen) return "busy";
    const w = writer.value;
    // Only the thread's first event is its thread_started, whichever process creates it.
    const first: readonly EventDraft[] =
      w.chain.fold.seq === 0 ? [await t.hosted.runner.started()] : [];
    // Checked again on the chain this lease holds: another host may have pinned another config
    // since target() looked, and nothing can be appended under it until release.
    if (first.length === 0 && !(await samePin(knownEvents(w.chain), t.hosted)))
      return "busy";
    const done = w.fenced(() => {
      const delivered = w.append([...first, delivery(next)]);
      if (!delivered.ok) return delivered;
      const cause = delivered.value.at(-1);
      if (cause?.kind !== "event") throw new Error("channel_delivery is known");
      return w.append(
        [input(next, cause.event.event_id)],
        consumes(db, next.inbox_id),
      );
    });
    if (!done.ok) return "busy";
  } finally {
    writer.value.release();
  }
  // The run goes on alone, its replies after it in the same lane (outbound.ts): awaited, it
  // would hold this thread's consumer, and a cancel sent during the run would wait for its end.
  void ctx.resume(t.hosted, tenant, next.item.principal, {
    id: t.threadId,
    branch: branchId,
  });
  return "done";
}

function delivery(next: Message): EventDraft {
  const { item } = next;
  const text =
    typeof item.content === "string"
      ? item.content
      : item.content
          .flatMap((p) => (p.type === "text" ? [p.text] : []))
          .join("\n");
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
  const main = log.mainBranch(t.threadId);
  const plan = await planOf(ctx, t, item);
  if (!main.ok || plan === undefined) {
    consumed(db, next.inbox_id, 0);
    return "done";
  }
  const alongside = consumes(db, next.inbox_id);
  const done = await control(log, main.value, item.principal, plan, {
    alongside,
  });
  if (!done.ok) {
    if (done.error.code === "branch_busy") return "busy";
    consumed(db, next.inbox_id, 0);
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
