import { type A2aFault, fault, type Task } from "@threadsai/a2a/protocol";
import {
  type BranchId,
  type EventId,
  type KnownEvent,
  knownEvents,
  openStore,
  type Principal,
  type ThreadId,
  threadHandle,
} from "threadsai/host";
import type { HostContext } from "../context";
import { outcomeFromLog } from "../outcome";
import { endOf } from "../subscribe";
import { openAsk } from "./question";
import { type Slice, taskOf } from "./state";

// The run slice a task is, as the log has it now. Every operation reads through here, so a
// GetTask, a stream frame and a two-host race all see one reading of one log.

export type Located = {
  readonly thread: ThreadId;
  readonly branch: BranchId;
  readonly runId: EventId;
};

/**
 * Every state the run has passed through, one slice per event of its own segment, oldest first.
 * Each slice's outcome and open question are computed over that prefix and no further, which is
 * what makes a frame a function of the log rather than of when it was read. The last one is the
 * task as it stands now.
 *
 * ponytail: the outcome is recomputed per prefix, so this is O(events²) per read. A run is tens to
 * hundreds of events; if a long one ever makes it matter, memoize by prefix length.
 */
export async function slicesOf(
  ctx: HostContext,
  tenant: string,
  at: Located,
): Promise<readonly Slice[] | undefined> {
  const { log } = await ctx.open(tenant);
  const read = await log.read(at.branch);
  if (!read.ok) return undefined;
  const events = knownEvents(read.value);
  const start = events.findIndex((e) => e.event_id === at.runId);
  if (start === -1) return undefined;
  const store = ctx.storeFor(tenant);
  const handle = threadHandle(await openStore(store), {
    id: at.thread,
    branch: at.branch,
    store,
  });
  const end = start + endOf(events, at.runId, start) + 1;
  const contextId = contextOf(events, at.runId) ?? at.thread;
  return events.slice(start, end).map((_, i) => {
    const upTo = events.slice(0, start + i + 1);
    return {
      taskId: at.runId,
      contextId,
      own: events.slice(start, start + i + 1),
      // `parked` only fills RunOutcome.pending, which A2A never shows, so the empty list here
      // costs nothing and saves re-folding the log at every prefix.
      outcome: outcomeFromLog(upTo, at.runId, [], handle),
      question: openAsk(upTo),
    };
  });
}

/** The run slice as it stands now, or undefined when the branch cannot be read. */
export async function sliceOf(
  ctx: HostContext,
  tenant: string,
  at: Located,
): Promise<Slice | undefined> {
  return (await slicesOf(ctx, tenant, at))?.at(-1);
}

/**
 * The one discriminator between an answer and a fault. No A2A result message has a `name` field,
 * so a fault is told apart by tag rather than by shape.
 */
export function isFault(value: object): value is A2aFault {
  return "name" in value && "message" in value;
}

export type Accepted = {
  readonly task: Task;
  /** Where the task's run lives; absent for a task we refused before it existed. */
  readonly at: Located | undefined;
};

/** The task as the log now shows it: what every operation answers with. */
export async function located(
  ctx: HostContext,
  principal: Principal,
  at: Located,
): Promise<Accepted | A2aFault> {
  const task = await taskAt(ctx, principal.tenant, at);
  return task === undefined
    ? fault("InternalError", `run ${at.runId} could not be read back`)
    : { task, at };
}

/** The slice as a Task, or undefined when the branch cannot be read. */
export async function taskAt(
  ctx: HostContext,
  tenant: string,
  at: Located,
): Promise<Task | undefined> {
  const slice = await sliceOf(ctx, tenant, at);
  return slice === undefined ? undefined : taskOf(slice);
}

/**
 * The contextId the run was started with, read back from its own user_input. That is how a retry
 * and every later GetTask answer the same contextId without a column for it.
 */
export function contextOf(
  events: readonly KnownEvent[],
  runId: EventId,
): string | undefined {
  const input = events.find(
    (e) => e.event_id === runId && e.type === "user_input",
  );
  return input?.type === "user_input" ? input.data.a2a?.context_id : undefined;
}

/** Whether the thread's branch has a run still going: one run at a time per context. */
export async function openRun(
  ctx: HostContext,
  principal: Principal,
  thread: ThreadId,
): Promise<boolean> {
  const { log } = await ctx.open(principal.tenant);
  const main = await log.mainBranch(thread);
  if (!main.ok) return false;
  const read = await log.read(main.value);
  if (!read.ok) return false;
  const events = knownEvents(read.value);
  const latest = events.findLast((e) => e.type === "user_input");
  if (latest === undefined) return false;
  const slice = await sliceOf(ctx, principal.tenant, {
    thread,
    branch: main.value,
    runId: latest.event_id,
  });
  const state = slice === undefined ? undefined : taskOf(slice).status.state;
  return state === "TASK_STATE_SUBMITTED" || state === "TASK_STATE_WORKING";
}
