import {
  type A2aFault,
  DEFAULT_PAGE_SIZE,
  fault,
  isTerminal,
  type ListTasksRequest,
  type ListTasksResponse,
  MAX_PAGE_SIZE,
  type Task,
} from "@threadsai/a2a/protocol";
import { openThread } from "threadsai";
import { type Principal, principalKey, storeConnection } from "threadsai/host";
import type { HostContext } from "../context";
import { resumeThread } from "../decisions";
import { a2aTask, a2aTasks, type TaskReceipt } from "../receipts";
import { a2aThreadId } from "./keys";
import { isFault, type Located, located, taskAt } from "./read";

// GetTask, ListTasks and CancelTask. Each resolves a task only through the caller's own receipts,
// so another principal's task is TaskNotFoundError and is indistinguishable from one that never
// existed. ListTasks is how a peer reconciles with us the same way we reconcile with a peer.

/** The task's run, found through the caller's own receipts. */
export async function locate(
  ctx: HostContext,
  principal: Principal,
  taskId: string,
): Promise<Located | A2aFault> {
  const { db } = await storeConnection(ctx.store);
  const found = await a2aTask(
    db,
    principal.tenant,
    principalKey(principal),
    taskId,
  );
  if (!found.ok) return fault("InternalError", found.error.message);
  const receipt = found.value;
  return receipt === undefined
    ? fault("TaskNotFoundError", `no task ${taskId}`)
    : {
        thread: receipt.thread_id,
        branch: receipt.branch_id,
        runId: receipt.run_id,
      };
}

export async function getTask(
  ctx: HostContext,
  principal: Principal,
  taskId: string,
): Promise<Task | A2aFault> {
  const at = await locate(ctx, principal, taskId);
  if (isFault(at)) return at;
  const task = await taskAt(ctx, principal.tenant, at);
  return task ?? fault("TaskNotFoundError", `no task ${taskId}`);
}

export async function listTasks(
  ctx: HostContext,
  principal: Principal,
  name: string,
  request: ListTasksRequest,
): Promise<ListTasksResponse | A2aFault> {
  const { db } = await storeConnection(ctx.store);
  const rows = await a2aTasks(db, principal.tenant, principalKey(principal));
  if (!rows.ok) return fault("InternalError", rows.error.message);
  // One task can hold several receipts (a send and each of its continuations), so the newest row
  // per run is the task, and a continued task is the most recently touched one.
  const mine = dedup(rows.value);
  // Filtered by deriving the context's thread id and comparing, because the derivation is one-way.
  const context = request.contextId;
  const wanted =
    context === undefined
      ? mine
      : mine.filter(
          (r) => r.thread_id === a2aThreadId(principal, name, context),
        );
  const size = pageSize(request.pageSize);
  const from = offset(request.pageToken);
  if (typeof from !== "number") return from;
  const page = wanted.slice(from, from + size);
  const tasks: Task[] = [];
  for (const row of page) {
    const task = await taskAt(ctx, principal.tenant, {
      thread: row.thread_id,
      branch: row.branch_id,
      runId: row.run_id,
    });
    // A row whose branch cannot be read is left out rather than failing the page.
    if (task !== undefined) tasks.push(task);
  }
  return {
    tasks,
    nextPageToken: from + size < wanted.length ? String(from + size) : "",
    pageSize: size,
    totalSize: wanted.length,
  };
}

function dedup(rows: readonly TaskReceipt[]): readonly TaskReceipt[] {
  const byRun = Map.groupBy(rows, (r) => r.run_id);
  return [...byRun.values()].flatMap((group) => group[0] ?? []);
}

function pageSize(asked: number | undefined): number {
  if (asked === undefined || asked <= 0) return DEFAULT_PAGE_SIZE;
  return Math.min(asked, MAX_PAGE_SIZE);
}

/** A page token is the offset it resumes at; anything else is a malformed cursor. */
function offset(token: string | undefined): number | A2aFault {
  if (token === undefined || token === "") return 0;
  const at = Number(token);
  return /^\d+$/.test(token) && Number.isSafeInteger(at)
    ? at
    : fault("InvalidParamsError", `pageToken ${token} is not a page token`);
}

/**
 * CancelTask: the durable barrier on the task's thread. The answer is the task in its current
 * state, which becomes CANCELED when the barrier applies. A cancel never claims to undo what
 * already happened.
 */
export async function cancelTask(
  ctx: HostContext,
  principal: Principal,
  taskId: string,
): Promise<Task | A2aFault> {
  const at = await locate(ctx, principal, taskId);
  if (isFault(at)) return at;
  const current = await taskAt(ctx, principal.tenant, at);
  if (current === undefined)
    return fault("TaskNotFoundError", `no task ${taskId}`);
  if (isTerminal(current.status.state))
    return fault(
      "TaskNotCancelableError",
      `task ${taskId} has already ended as ${current.status.state}`,
    );
  const opened = await openThread(ctx.storeFor(principal.tenant), at.thread, {
    branchId: at.branch,
  });
  if (!opened.ok) return fault("InternalError", opened.error.message);
  const done = await opened.value.cancel(principal);
  if (!done.ok) return fault("InternalError", done.error.message);
  await resumeThread(ctx, principal, opened.value);
  const answered = await located(ctx, principal, at);
  return isFault(answered) ? answered : answered.task;
}
