import { openThread, type Thread } from "@threads/core";
import {
  type KnownEvent,
  knownEvents,
  loopParked,
  type ParkAddress,
  storeConnection,
  type ThreadId,
} from "@threads/core/host";
import type { HostContext, HostedAgent } from "../context";
import { failure } from "../errors";
import { uiReceipts } from "../receipts";
import type { Call } from "../threads";

// What the UI routes share: the route's agent, the key's thread and its log, the thread's `ui`
// receipts, and the failure codes a start-and-stream route answers before its stream starts.

export const UI_CODES: readonly string[] = [
  "unauthenticated",
  "forbidden",
  "invalid_request",
  "not_found",
  "branch_busy",
  "branch_not_runnable",
  "idempotency_key_reused",
  "approval_mismatch",
  "approval_expired",
  "approval_duplicate",
  "no_open_question",
];

export function routeAgent(call: Call): HostedAgent | Response {
  const name = call.params["agent"] ?? "";
  return call.ctx.agents.get(name) ?? failure("not_found", `no agent ${name}`);
}

/** A UI thread's handle and its main branch's log as last read. */
export type Log = {
  readonly thread: Thread;
  readonly events: readonly KnownEvent[];
  readonly parked: readonly ParkAddress[];
};

/** The thread's handle and its main branch's log, or undefined when it doesn't exist yet. */
export async function uiThread(
  call: Call,
  threadId: ThreadId,
): Promise<Log | undefined> {
  const opened = await openThread(
    call.ctx.storeFor(call.principal.tenant),
    threadId,
  );
  return opened.ok
    ? readLog(call.ctx, call.principal.tenant, opened.value)
    : undefined;
}

/** The thread's log read again, as it stands now. */
export async function readLog(
  ctx: HostContext,
  tenant: string,
  thread: Thread,
): Promise<Log | undefined> {
  const { log } = await ctx.open(tenant);
  const read = await log.read(thread.branch);
  if (!read.ok) return undefined;
  return {
    thread,
    events: knownEvents(read.value),
    parked: loopParked(read.value.fold),
  };
}

/**
 * The codes a decision or answer gets when someone else settled the interrupt between the
 * route's log read and its append: the route reads the log again and treats it as settled.
 */
export const SETTLED_MEANWHILE: ReadonlySet<string> = new Set([
  "approval_duplicate",
  "approval_expired",
  "no_open_question",
]);

export async function receiptsOf(
  ctx: HostContext,
  tenant: string,
  threadId: ThreadId,
): Promise<ReadonlyMap<string, string>> {
  const { db } = await storeConnection(ctx.store);
  const found = await uiReceipts(db, tenant, threadId);
  return found.ok ? found.value : new Map();
}
