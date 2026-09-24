import type { BranchId } from "../../log";
import { ok } from "../../result";
import type { LogStore } from "../../store";
import { liveWriter } from "../../store/live";
import { type DecideTx, isRefusal } from "../../store/writer";
import { Batch, type Mint } from "../../team/batch";
import { TEAM_CONSTANTS } from "../../team/constants";

// The team log's writer for operator requests (design §4.3, Writes): this process's live writer
// of the branch when it has one, else the lease, taken and handed back per request. A held lease
// is retried with backoff until the busy bound, then busy, which writes nothing (design §4.2).

export const BUSY = "busy";

/** Writer loss: a newer holder took the lease or the head moved. Re-acquire and decide again. */
const LOST: ReadonlySet<string> = new Set([
  "stale_epoch",
  "seq_conflict",
  "writer_poisoned",
]);
const FIRST_PAUSE_MS = 10;

export type TeamLogOptions = {
  readonly busyBoundMs?: number;
  readonly mint?: Mint;
};

/**
 * One decided append on the team log: `decide` adds the request's drafts to the batch and returns
 * its outcome. Decided again from scratch after writer loss; `busy` once the bound has passed.
 */
export async function onTeamLog<T>(
  log: LogStore,
  branch: BranchId,
  decide: (tx: DecideTx, batch: Batch) => T,
  options: TeamLogOptions = {},
): Promise<T | typeof BUSY> {
  const bound = options.busyBoundMs ?? TEAM_CONSTANTS.busyBoundMs;
  const deadline = Date.now() + bound;
  for (let pause = FIRST_PAUSE_MS; ; pause *= 2) {
    const done = once(log, branch, decide, options.mint);
    if (done !== undefined) return done.value;
    const left = deadline - Date.now();
    if (left <= 0) return BUSY;
    const wait = Math.min(pause, left, TEAM_CONSTANTS.wakePollInProcessMs);
    await new Promise((resolve) => setTimeout(resolve, wait));
  }
}

/** One attempt: its outcome, or undefined when the lease was held or lost. */
function once<T>(
  log: LogStore,
  branch: BranchId,
  decide: (tx: DecideTx, batch: Batch) => T,
  mint: Mint | undefined,
): { readonly value: T } | undefined {
  const live = liveWriter(branch);
  const taken =
    live === undefined
      ? log.acquire(branch, `operator-${crypto.randomUUID()}`)
      : ok(live);
  if (!taken.ok) {
    if (taken.error.code === "branch_busy") return undefined;
    throw new Error(`team log ${branch}: ${taken.error.message}`);
  }
  const writer = taken.value;
  try {
    const out: { value?: { readonly value: T } } = {};
    const appended = writer.appendDecided((tx) => {
      const batch = new Batch(tx.chain.fold.seq, tx.now, mint);
      out.value = { value: decide(tx, batch) };
      return ok(batch.drafts);
    });
    if (isRefusal(appended))
      throw new Error("an operator request never refuses its append");
    if (!appended.ok) {
      if (LOST.has(appended.error.code)) return undefined;
      throw new Error(`team log ${branch}: ${appended.error.message}`);
    }
    if (out.value === undefined) throw new Error("the append decided nothing");
    return out.value;
  } finally {
    if (live === undefined) writer.release();
  }
}
