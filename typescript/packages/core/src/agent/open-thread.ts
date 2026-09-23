import { BranchId, ThreadId } from "../log";
import type { EventDraft, LogStore } from "../store";
import { uuidv7 } from "../store/encode";
import { ConfigError } from "./errors";
import type { ThreadRef } from "./result";

// Opening a run's thread: a new one, a child or handoff target created on first use under its
// fixed id, or the lease on an existing one's branch.

type Opened = {
  readonly threadId: ThreadId;
  readonly branchId: BranchId;
  readonly writer: ReturnType<LogStore["acquire"]>;
};

/**
 * A new thread (its header and thread_started), or the lease on an existing one's branch. With
 * `create`, a thread id that isn't in the store yet is created under that id (a child).
 */
export function open(
  log: LogStore,
  thread: ThreadId | ThreadRef | undefined,
  first: readonly EventDraft[],
  holder: string,
  create: boolean,
): Opened {
  if (thread === undefined)
    return created(log, ThreadId.parse(uuidv7(Date.now())), first, holder);
  const threadId = typeof thread === "string" ? thread : thread.id;
  const branch =
    typeof thread === "string"
      ? log.mainBranch(thread)
      : { ok: true as const, value: thread.branch };
  if (!branch.ok && create) return created(log, threadId, first, holder);
  if (!branch.ok)
    throw new ConfigError(
      "invalid_config",
      `thread ${threadId} is not in this store`,
    );
  const writer = log.acquire(branch.value, holder);
  // A crash between createBranch and the first append left the thread empty: start it now.
  if (create && writer.ok && writer.value.chain.fold.seq === 0) {
    const appended = writer.value.append(first);
    if (!appended.ok)
      return { threadId, branchId: branch.value, writer: appended };
  }
  return { threadId, branchId: branch.value, writer };
}

function created(
  log: LogStore,
  threadId: ThreadId,
  first: readonly EventDraft[],
  holder: string,
): Opened {
  const branchId = BranchId.parse(uuidv7(Date.now()));
  const made = log.createBranch(threadId, branchId);
  if (!made.ok) return { threadId, branchId, writer: made };
  const writer = log.acquire(branchId, holder);
  if (writer.ok) {
    const appended = writer.value.append(first);
    if (!appended.ok) return { threadId, branchId, writer: appended };
  }
  return { threadId, branchId, writer };
}
