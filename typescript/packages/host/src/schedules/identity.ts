import {
  BranchId,
  type EventDraft,
  knownEvents,
  type LogStore,
  renewTeam,
  type StoreDriver,
  ThreadId,
  type Tx,
  uuidv7,
} from "@threads/core/host";
import { pinMatches } from "../context";
import {
  currentThread,
  type Due,
  insertPending,
  makeCurrent,
  pendingOf,
  reserved,
} from "./rows";

// Which thread a schedule's occurrences go to. A schedule keeps its thread while its agent pins
// the same config; a config change moves it to a new thread once the old one is quiet.

/**
 * Reserves due occurrences on the schedule's thread, in one transaction with finding that thread:
 * a deletion commits wholly before (a new thread is made) or after (these rows are retired). A
 * schedule without a thread gets one; so does one whose agent now pins another config than its
 * thread's, once that thread is quiet. Keys another scheduler reserved are skipped first, so a
 * losing scheduler never moves the schedule.
 */
export async function reserveDue(
  db: StoreDriver,
  log: LogStore,
  started: EventDraft,
  due: readonly Due[],
): Promise<void> {
  // Retry-safe: each attempt picks and makes its thread afresh, from committed rows.
  await db.transaction(async (tx) => {
    const fresh: Due[] = [];
    for (const row of due)
      if (!(await reserved(tx, log.tenant, row))) fresh.push(row);
    const first = fresh[0];
    if (first === undefined) return;
    const inTx = log.within(tx);
    const found = await currentThread(tx, log.tenant, first.schedule_id);
    const threadId =
      found !== undefined && (await keeps(tx, inTx, found, started))
        ? found
        : await newThread(tx, inTx, first.schedule_id, started);
    for (const row of fresh)
      await insertPending(tx, log.tenant, threadId, row, log.now());
  });
}

/**
 * Whether the schedule stays on its thread: it pins the same config, or it doesn't but the
 * thread is still busy. A config change moves to a new thread only once the old one is quiet (no
 * open turn, no undecided reservation), so no new run starts while the old one goes on.
 */
async function keeps(
  tx: Tx,
  log: LogStore,
  threadId: ThreadId,
  started: EventDraft,
): Promise<boolean> {
  const main = await log.mainBranch(threadId);
  const read = main.ok ? await log.read(main.value) : undefined;
  // An unreadable thread can't be shown quiet: the reservation fails, and the identity stays.
  if (read?.ok !== true)
    throw new Error(`schedule thread ${threadId} can't be read`);
  if (pinMatches(knownEvents(read.value), started)) return true;
  return (
    read.value.fold.turnOpen ||
    (await pendingOf(tx, log.tenant, threadId)).length > 0
  );
}

/** A new current thread for the schedule, in the caller's transaction, through the log store. */
async function newThread(
  tx: Tx,
  log: LogStore,
  scheduleId: string,
  started: EventDraft,
): Promise<ThreadId> {
  const threadId = ThreadId.parse(uuidv7(log.now()));
  const branchId = BranchId.parse(uuidv7(log.now()));
  await makeCurrent(tx, log.tenant, scheduleId, threadId, log.now());
  must(await log.createBranch(threadId, branchId));
  const writer = must(
    await log.acquire(branchId, `schedule-${uuidv7(log.now())}`),
  );
  // A lead's new thread names a new team of its own, which its first append opens.
  must(await writer.append([renewTeam(started, log.now())]));
  await writer.release();
  return threadId;
}

/** A new branch takes its first line: anything else is a bug, not an outcome. */
function must<T>(
  result:
    | { readonly ok: true; readonly value: T }
    | { readonly ok: false; readonly error: { readonly message: string } },
): T {
  if (!result.ok) throw new Error(result.error.message);
  return result.value;
}
