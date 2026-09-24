import { Input } from "@threads/core";
import {
  canonicalize,
  Int,
  JsonValue,
  NonEmpty,
  parseRows,
  type SqliteDriver,
  ThreadId,
} from "@threads/core/host";
import { z } from "zod";

// The scheduler's rows (spec/schema/store.sql): the threads a schedule has had
// (schedule_threads) and its occurrences (schedule_occurrences), every read and write scoped by
// tenant. SQL only: which thread a schedule uses is decided in identity.ts.

const REASONS = ["missed", "overlap", "removed"] as const;
export type Reason = (typeof REASONS)[number];

/** A reserved occurrence not yet decided, with what firing it needs frozen at reservation. */
export type Pending = {
  readonly schedule_id: string;
  readonly occurrence_at: number;
  readonly thread_id: ThreadId;
  readonly reason: Reason | null;
  readonly agent: string;
  readonly input: Input;
  readonly timezone: string;
};

/** A due occurrence to reserve on the schedule's thread. */
export type Due = Omit<Pending, "thread_id" | "reason"> & {
  readonly missed: boolean;
};

/** The frozen input, stored as canonical JSON: a row whose input doesn't parse is corrupt. */
const StoredInput = z.string().transform((text, ctx): Input => {
  const parsed = Input.safeParse(json(text));
  if (parsed.success) return parsed.data;
  ctx.addIssue({ code: "custom", message: "a stored input is not an Input" });
  return z.NEVER;
});

const Row: z.ZodType<Pending> = z
  .strictObject({
    schedule_id: NonEmpty,
    occurrence_at: Int,
    thread_id: ThreadId,
    reason: z.enum(REASONS).nullable(),
    agent: NonEmpty,
    input_json: StoredInput,
    timezone: NonEmpty,
  })
  .transform(({ input_json, ...row }) => ({ ...row, input: input_json }));

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

/** Whether the occurrence's key is already reserved, in any state. */
export function reserved(db: SqliteDriver, tenant: string, row: Due): boolean {
  return (
    db.all(
      `SELECT 1 FROM schedule_occurrences
        WHERE tenant_id = ? AND schedule_id = ? AND occurrence_at = ?`,
      [tenant, row.schedule_id, row.occurrence_at],
    ).length > 0
  );
}

/** Inserts a pending row; a key another scheduler reserved first wins silently. */
export function insertPending(
  db: SqliteDriver,
  tenant: string,
  threadId: ThreadId,
  row: Due,
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
      threadId,
      now,
      row.agent,
      canonical(row.input),
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

/** Every thread the tenant's schedules have had: what recovery walks. */
export function scheduleThreads(
  db: SqliteDriver,
  tenant: string,
): readonly ThreadId[] {
  return rows(
    z.strictObject({ thread_id: ThreadId }),
    db.all(
      "SELECT DISTINCT thread_id FROM schedule_threads WHERE tenant_id = ?",
      [tenant],
    ),
  ).map((r) => r.thread_id);
}

/** The schedule's current thread. */
export function currentThread(
  db: SqliteDriver,
  tenant: string,
  scheduleId: string,
): ThreadId | undefined {
  const [found] = rows(
    z.strictObject({ thread_id: ThreadId }),
    db.all(
      `SELECT thread_id FROM schedule_threads
        WHERE tenant_id = ? AND schedule_id = ? AND current = 1`,
      [tenant, scheduleId],
    ),
  );
  return found?.thread_id;
}

/** Makes `threadId` the schedule's current thread; the old one stays listed for recovery. */
export function makeCurrent(
  db: SqliteDriver,
  tenant: string,
  scheduleId: string,
  threadId: ThreadId,
  now: number,
): void {
  db.run(
    `UPDATE schedule_threads SET current = 0
      WHERE tenant_id = ? AND schedule_id = ? AND current = 1`,
    [tenant, scheduleId],
  );
  db.run(
    `INSERT INTO schedule_threads (tenant_id, schedule_id, thread_id, current, created_at)
      VALUES (?, ?, ?, 1, ?)`,
    [tenant, scheduleId, threadId, now],
  );
}

function canonical(input: Input): string {
  const text = canonicalize(JsonValue.parse(input));
  if (!text.ok) throw new Error("a parsed input is canonical JSON");
  return text.value;
}

function json(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

/** Stored rows, parsed: a row that fails its schema is corruption, reported, never trusted. */
function rows<T>(
  schema: z.ZodType<T>,
  found: readonly unknown[],
): readonly T[] {
  const parsed = parseRows(schema, found);
  if (!parsed.ok)
    throw new Error(`schedule rows are corrupt: ${parsed.error.message}`);
  return parsed.value;
}
