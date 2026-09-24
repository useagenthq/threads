import { openStore, type Store } from "../agent/sqlite";
import type { BranchId, ThreadId } from "../log";
import { err, ok, type Result } from "../result";
import type { LogStore } from "../store";
import { type LogError, logError } from "../verify";
import { type HandleOptions, type Thread, threadHandle } from "./handle";
import { readLog } from "./read";

// openThread() (spec/api.json): a handle for inspection and control that reads
// through the store and needs no agent in memory.

export type OpenThreadOptions = HandleOptions & {
  readonly branchId?: BranchId;
};

/** The listed branch of this thread: ready or inspection-only, never forking or failed. */
function listedBranch(
  log: LogStore,
  threadId: ThreadId,
  branchId: BranchId | undefined,
): Result<BranchId, LogError> {
  const branch =
    branchId === undefined ? log.mainBranch(threadId) : ok(branchId);
  const state = branch.ok ? log.branchState(branch.value) : branch;
  const listed =
    state.ok && (state.value === "ready" || state.value === "inspection_only");
  return branch.ok && listed
    ? branch
    : err(logError("not_found", `no thread ${threadId}`));
}

export async function openThread(
  store: Store,
  threadId: ThreadId,
  options: OpenThreadOptions = {},
): Promise<Result<Thread, LogError>> {
  const opened = await openStore(store);
  const branch = listedBranch(opened.log, threadId, options.branchId);
  if (!branch.ok) return branch;
  const read = readLog(opened.log, branch.value);
  if (!read.ok) return read;
  if (read.value.segments[0]?.header.thread_id !== threadId)
    return err(logError("not_found", `no thread ${threadId}`));
  const sandbox =
    options.sandbox === undefined ? {} : { sandbox: options.sandbox };
  return ok(
    threadHandle(
      opened,
      { id: threadId, branch: branch.value, store },
      sandbox,
    ),
  );
}
