import { openThread, type Thread } from "@threads/core";
import {
  type KnownEvent,
  knownEvents,
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

/** The thread's handle and its main branch's log, or undefined when it doesn't exist yet. */
export async function uiThread(
  call: Call,
  threadId: ThreadId,
): Promise<
  | {
      readonly thread: Thread;
      readonly events: readonly KnownEvent[];
      readonly parked: readonly ParkAddress[];
    }
  | undefined
> {
  const opened = await openThread(
    call.ctx.storeFor(call.principal.tenant),
    threadId,
  );
  if (!opened.ok) return undefined;
  const { log } = await call.ctx.open(call.principal.tenant);
  const read = log.read(opened.value.branch);
  if (!read.ok) return undefined;
  return {
    thread: opened.value,
    events: knownEvents(read.value),
    parked: read.value.fold.parked,
  };
}

export async function receiptsOf(
  ctx: HostContext,
  tenant: string,
  threadId: ThreadId,
): Promise<ReadonlyMap<string, string>> {
  const { db } = await storeConnection(ctx.store);
  const found = uiReceipts(db, tenant, threadId);
  return found.ok ? found.value : new Map();
}
