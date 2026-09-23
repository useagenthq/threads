import {
  BranchId,
  type EventDraft,
  Int,
  type LogStore,
  parseRows,
  type SqliteDriver,
  ThreadId,
  uuidv7,
} from "@threads/core/host";
import { z } from "zod";

// The scheduler's rows (spec/schema/store.sql): a schedule's one thread (schedule_threads) and its
// occurrences (schedule_occurrences), every read and write scoped by tenant.

const REASONS = ["missed", "overlap", "removed"] as const;
export type Reason = (typeof REASONS)[number];

/** A reserved occurrence not yet decided, with what firing it needs frozen at reservation. */
export type Pending = {
  readonly schedule_id: string;
  readonly occurrence_at: number;
  readonly thread_id: ThreadId;
  readonly reason: Reason | null;
  readonly agent: string;
  readonly input_json: string;
  readonly timezone: string;
};

const Row: z.ZodType<Pending> = z.strictObject({
  schedule_id: z.string(),
  occurrence_at: Int,
  thread_id: ThreadId,
  reason: z.enum(REASONS).nullable(),
  agent: z.string(),
  input_json: z.string(),
  timezone: z.string(),
});

const PENDING = `SELECT schedule_id, occurrence_at, thread_id, reason, agent, input_json, timezone
  FROM schedule_occurrences WHERE tenant_id = ? AND state = 'pending'`;

/** The tenant's pending rows in occurrence order, whatever schedules are configured now. */
export function pendingRows(
  db: SqliteDriver,
  tenant: string,
): readonly Pending[] {
  return rows(
    Row,
    db.all(`${PENDING} ORDER BY occurrence_at, thread_id, schedule_id`, [
      tenant,
    ]),
  );
}

/** One thread's pending rows, in occurrence order. */
export function pendingOf(
  db: SqliteDriver,
  tenant: string,
  threadId: ThreadId,
): readonly Pending[] {
  return rows(
    Row,
    db.all(
      `${PENDING} AND thread_id = ? ORDER BY occurrence_at, thread_id, schedule_id`,
      [tenant, threadId],
    ),
  );
}

/** Reserves a due occurrence; another scheduler's reservation of the same key wins silently. */
export function reserve(
  db: SqliteDriver,
  tenant: string,
  row: Omit<Pending, "reason"> & { readonly missed: boolean },
  now: number,
): void {
  db.run(
    `INSERT INTO schedule_occurrences (tenant_id, schedule_id, occurrence_at, state, reason,
      thread_id, claimed_at, agent, input_json, timezone)
      VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING`,
    [
      tenant,
      row.schedule_id,
      row.occurrence_at,
      row.missed ? "missed" : null,
      row.thread_id,
      now,
      row.agent,
      row.input_json,
      row.timezone,
    ],
  );
}

/** Marks a thread's undecided pending rows as overlaps: its log shows a turn still open. */
export function markOverlaps(
  db: SqliteDriver,
  tenant: string,
  threadId: ThreadId,
): void {
  db.run(
    `UPDATE schedule_occurrences SET reason = 'overlap'
      WHERE tenant_id = ? AND thread_id = ? AND state = 'pending' AND reason IS NULL`,
    [tenant, threadId],
  );
}

/**
 * Decides a pending row, in the transaction of the append that logs it at `seq`. False when it
 * is no longer pending (another scheduler decided it): the caller rolls its append back.
 */
export function decide(
  db: SqliteDriver,
  tenant: string,
  row: Pending,
  outcome: { readonly reason: Reason | null; readonly seq: number },
): boolean {
  return (
    db.all(
      `UPDATE schedule_occurrences SET state = ?, reason = ?, logged_seq = ?
        WHERE tenant_id = ? AND schedule_id = ? AND occurrence_at = ? AND state = 'pending'
        RETURNING occurrence_at`,
      [
        outcome.reason === null ? "fired" : "skipped",
        outcome.reason,
        outcome.seq,
        tenant,
        row.schedule_id,
        row.occurrence_at,
      ],
    ).length === 1
  );
}

/** The latest reserved occurrence of a schedule, in any state. */
export function lastOccurrence(
  db: SqliteDriver,
  tenant: string,
  scheduleId: string,
): number | undefined {
  const [last] = rows(
    z.strictObject({ at: Int.nullable() }),
    db.all(
      `SELECT max(occurrence_at) AS at FROM schedule_occurrences
        WHERE tenant_id = ? AND schedule_id = ?`,
      [tenant, scheduleId],
    ),
  );
  return last?.at ?? undefined;
}

/** The threads of the tenant's schedules. */
export function scheduleThreads(
  db: SqliteDriver,
  tenant: string,
): readonly ThreadId[] {
  return rows(
    z.strictObject({ thread_id: ThreadId }),
    db.all("SELECT thread_id FROM schedule_threads WHERE tenant_id = ?", [
      tenant,
    ]),
  ).map((r) => r.thread_id);
}

function threadOf(
  db: SqliteDriver,
  tenant: string,
  scheduleId: string,
): ThreadId | undefined {
  const [found] = rows(
    z.strictObject({ thread_id: ThreadId }),
    db.all(
      "SELECT thread_id FROM schedule_threads WHERE tenant_id = ? AND schedule_id = ?",
      [tenant, scheduleId],
    ),
  );
  return found?.thread_id;
}

class Lost extends Error {}

/**
 * The schedule's thread, created on first use in one transaction with its identity row, branch
 * and `first` event (its thread_started). A scheduler that loses the identity insert rolls all of
 * it back and uses the winner's thread, so there is never an orphan branch or a second thread.
 */
export async function scheduleThread(
  db: SqliteDriver,
  log: LogStore,
  scheduleId: string,
  first: () => Promise<EventDraft>,
): Promise<ThreadId> {
  const found = threadOf(db, log.tenant, scheduleId);
  if (found !== undefined) return found;
  const draft = await first();
  const threadId = ThreadId.parse(uuidv7(log.now()));
  const branchId = BranchId.parse(uuidv7(log.now()));
  try {
    return db.transaction(() => {
      const won = db.all(
        `INSERT INTO schedule_threads (tenant_id, schedule_id, thread_id, created_at)
          VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING RETURNING thread_id`,
        [log.tenant, scheduleId, threadId, log.now()],
      );
      if (won.length === 0) throw new Lost();
      must(log.createBranch(threadId, branchId));
      const writer = must(
        log.acquire(branchId, `schedule-${uuidv7(log.now())}`),
      );
      must(writer.append([draft]));
      writer.release();
      return threadId;
    });
  } catch (error) {
    if (!(error instanceof Lost)) throw error;
    const winner = threadOf(db, log.tenant, scheduleId);
    if (winner === undefined) throw new Error("a lost insert has a winner");
    return winner;
  }
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

/** Stored rows, parsed: a row that fails its schema is corruption, never trusted. */
function rows<T>(
  schema: z.ZodType<T>,
  found: readonly unknown[],
): readonly T[] {
  const parsed = parseRows(schema, found);
  if (!parsed.ok) throw new Error(parsed.error.message);
  return parsed.value;
}
