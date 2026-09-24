import {
  type BranchId,
  type EventId,
  err,
  type KnownEvent,
  knownEvents,
  type LogStore,
  ok,
  openStore,
  type Principal,
  type Result,
  runEnd,
  type ThreadId,
  threadHandle,
} from "@threads/core/host";
import type { HostContext } from "./context";
import { outcomeFromLog, type RunOutcome, toOutcome } from "./outcome";

// Host.subscribe (GET /v1/threads/{thread_id}/runs/{run_id}/events): follows one run from the
// log, its committed events and then one result naming run_id. It takes no input and starts
// nothing; it reads the log, it is not a second loop.

export type SseMessage =
  | { readonly kind: "event"; readonly event: KnownEvent }
  | {
      readonly kind: "result";
      readonly run_id: EventId;
      readonly result: RunOutcome;
    };

type SubscribeError = {
  readonly code: "forbidden" | "not_found";
  readonly message: string;
};

const POLL_MS = 50;

export async function subscribe(
  ctx: HostContext,
  threadId: ThreadId,
  runId: EventId,
  principal: Principal,
  afterSeq = 0,
): Promise<Result<AsyncIterable<SseMessage>, SubscribeError>> {
  const { log } = await ctx.open(principal.tenant);
  const branch = runBranch(log, threadId, runId);
  if (branch === undefined)
    return err({ code: "not_found", message: `no run ${runId}` });
  return ok(follow(ctx, log, { id: threadId, branch }, runId, afterSeq));
}

/** The branch whose own segment holds the run's user_input. */
function runBranch(
  log: LogStore,
  threadId: ThreadId,
  runId: EventId,
): BranchId | undefined {
  const listed = log.branches(threadId);
  if (!listed.ok) return undefined;
  for (const { branch_id } of listed.value) {
    const read = log.read(branch_id);
    if (!read.ok) continue;
    const found = knownEvents(read.value).some(
      (e) =>
        e.event_id === runId &&
        e.type === "user_input" &&
        e.branch_id === branch_id,
    );
    if (found) return branch_id;
  }
  return undefined;
}

async function* follow(
  ctx: HostContext,
  log: LogStore,
  thread: { readonly id: ThreadId; readonly branch: BranchId },
  runId: EventId,
  afterSeq: number,
): AsyncIterable<SseMessage> {
  let cursor = afterSeq;
  const store = ctx.storeFor(log.tenant);
  const handle = threadHandle(await openStore(store), { ...thread, store });
  for (;;) {
    const read = log.read(thread.branch);
    if (!read.ok) return;
    const events = knownEvents(read.value);
    const start = events.findIndex((e) => e.event_id === runId);
    const own = events.slice(start);
    const result =
      outcomeFromLog(events, runId, read.value.fold.parked, handle) ??
      halted(ctx, runId);
    const end = endOf(events, runId, start) + 1;
    for (const event of own.slice(0, end))
      if (event.seq > cursor) {
        cursor = event.seq;
        yield { kind: "event", event };
      }
    if (result !== undefined) {
      yield { kind: "result", run_id: runId, result };
      return;
    }
    const { promise, resolve } = Promise.withResolvers<void>();
    setTimeout(resolve, POLL_MS);
    await promise;
  }
}

/**
 * The run's last event, relative to its user_input at `start`: where it ended, else (running,
 * parked or halted) the one before the next input, else the latest. The stream never shows
 * another run's events.
 */
function endOf(
  events: readonly KnownEvent[],
  runId: EventId,
  start: number,
): number {
  const { at } = runEnd(events, runId);
  if (at !== undefined) return at - start;
  const own = events.slice(start);
  const next = own.slice(1).findIndex((e) => e.type === "user_input");
  return next === -1 ? own.length - 1 : next;
}

/** A run this process executed that halted before its turn could complete (branch_busy). */
function halted(ctx: HostContext, runId: EventId): RunOutcome | undefined {
  const result = ctx.results.get(runId);
  return result?.status === "failed" ? toOutcome(result) : undefined;
}
