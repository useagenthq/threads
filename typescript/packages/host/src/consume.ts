import { type ChannelAdapter, openThread } from "@threads/core";
import {
  assertNever,
  BranchId,
  type EventDraft,
  type EventId,
  storeConnection,
  type ThreadId,
  uuidv7,
} from "@threads/core/host";
import type { HostContext, HostedAgent } from "./context";
import {
  type Conversation,
  consumed,
  type InboxItem,
  pendingItems,
} from "./inbox";

// The run side of channel intake: items leave the inbox under the
// branch lease, a message as channel_delivery then user_input{source: channel}, a decision as
// an approval by an authorized approver, a control as a cancel. Each item is consumed once.

type Message = InboxItem & { readonly item: { readonly kind: "message" } };

/** Consumes the thread's items in order until none is left or its branch is busy. */
export async function consume(
  ctx: HostContext,
  tenant: string,
  threadId: ThreadId,
): Promise<void> {
  const { db } = await storeConnection(ctx.store);
  for (;;) {
    const pending = pendingItems(db, tenant, threadId);
    const next = pending.ok ? pending.value[0] : undefined;
    if (next === undefined) return;
    const adapter = ctx.channels.get(next.channel);
    const hosted =
      adapter === undefined ? undefined : ctx.agents.get(adapter.agent);
    if (adapter === undefined || hosted === undefined) {
      consumed(db, next.inbox_id, 0);
      continue;
    }
    const conversation = {
      tenant,
      channel: next.channel,
      installation: next.installation_id,
      address: next.item.address,
    };
    const going = await item(
      ctx,
      { adapter, hosted, conversation, threadId },
      next,
    );
    if (going === "busy") return;
  }
}

type Target = {
  readonly adapter: ChannelAdapter;
  readonly hosted: HostedAgent;
  readonly conversation: Conversation;
  readonly threadId: ThreadId;
};

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
      await control(ctx, t, next);
      return "done";
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
  const branchId = main.ok ? main.value : BranchId.parse(uuidv7(log.now()));
  if (!main.ok) {
    const made = log.createBranch(t.threadId, branchId);
    if (!made.ok) return "busy";
  }
  const first: readonly EventDraft[] = main.ok
    ? []
    : [await t.hosted.runner.started()];
  const writer = log.acquire(branchId, `host-${crypto.randomUUID()}`);
  if (!writer.ok) return "busy";
  try {
    if (writer.value.chain.fold.turnOpen) return "busy";
    const w = writer.value;
    const done = w.fenced(() => {
      const delivered = w.append([...first, delivery(next)]);
      if (!delivered.ok) return delivered;
      const cause = delivered.value.at(-1);
      if (cause?.kind !== "event") throw new Error("channel_delivery is known");
      return w.append([input(next, cause.event.event_id)], (added) => {
        const run = added.at(-1);
        if (run?.kind === "event") consumed(db, next.inbox_id, run.event.seq);
        return { ok: true, value: undefined };
      });
    });
    if (!done.ok) return "busy";
  } finally {
    writer.value.release();
  }
  // The run's replies follow it in the same lane (outbound.ts).
  await ctx.resume(t.hosted, tenant, next.item.principal, {
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
 * A decision answers a challenge only from an approver the agent's policy names, arriving in
 * the conversation's own installation (its thread); without configured approvers, channel
 * approvals are refused. A control cancels. Either is consumed once.
 */
async function control(
  ctx: HostContext,
  t: Target,
  next: InboxItem,
): Promise<void> {
  const { tenant } = t.conversation;
  const { db } = await storeConnection(ctx.store);
  consumed(db, next.inbox_id, 0);
  const thread = await openThread(ctx.storeFor(tenant), t.threadId);
  if (!thread.ok) return;
  const { item } = next;
  const done = await applied(ctx, t, thread.value, item);
  if (!done) return;
  await ctx.resume(t.hosted, tenant, item.principal, {
    id: thread.value.id,
    branch: thread.value.branch,
  });
}

type Thread = Extract<
  Awaited<ReturnType<typeof openThread>>,
  { ok: true }
>["value"];

async function applied(
  ctx: HostContext,
  t: Target,
  thread: Thread,
  item: InboxItem["item"],
): Promise<boolean> {
  switch (item.kind) {
    case "decision": {
      if (!ctx.mayApprove(t.hosted, item.principal, "channel")) return false;
      const done =
        item.decision === "grant"
          ? await thread.approve(item.challenge_id, item.principal)
          : await thread.deny(item.challenge_id, item.principal);
      return done.ok;
    }
    case "control":
      return (
        item.command === "cancel" && (await thread.cancel(item.principal)).ok
      );
    case "message":
      return false;
    default:
      return assertNever(item);
  }
}
