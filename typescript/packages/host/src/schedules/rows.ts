import { Input } from "@threads/core";
import {
  BranchId,
  canonicalize,
  type EventDraft,
  Int,
  JsonValue,
  knownEvents,
  type LogStore,
  NonEmpty,
  parseRows,
  type SqliteDriver,
  ThreadId,
  uuidv7,
  type VerifiedLog,
} from "@threads/core/host";
import { z } from "zod";

// The scheduler's rows (spec/schema/store.sql): a schedule's thread (schedule_threads) and its
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

/**
 * Reserves due occurrences on the schedule's thread, in one transaction with finding that thread:
 * a deletion commits wholly before (a new thread is made) or after (these rows are retired). A
 * schedule without a thread gets one; so does one whose agent now pins another config than its
 * thread's (a config change starts a new thread), once that thread is quiet. The identity row,
 * branch and thread_started are written together. Keys another scheduler reserved are skipped.
 */
export function reserveDue(
  db: SqliteDriver,
  log: LogStore,
  started: EventDraft,
  due: readonly Due[],
): void {
  db.transaction(() => {
    const fresh = due.filter((row) => !reserved(db, log.tenant, row));
    const first = fresh[0];
    if (first === undefined) return;
    const found = threadOf(db, log.tenant, first.schedule_id);
    const threadId =
      found !== undefined && keeps(db, log, found, started)
        ? found
        : newThread(db, log, first.schedule_id, started);
    for (const row of fresh)
      db.run(
        `INSERT INTO schedule_occurrences (tenant_id, schedule_id, occurrence_at, state, reason,
          thread_id, claimed_at, agent, input_json, timezone)
          VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING`,
        [
          log.tenant,
          row.schedule_id,
          row.occurrence_at,
          row.missed ? "missed" : null,
          threadId,
          log.now(),
          row.agent,
          canonical(row.input),
          row.timezone,
        ],
      );
  });
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

/** The config_hash a draft thread_started pins. */
export function hashOf(started: EventDraft): string | undefined {
  return started.type === "thread_started"
    ? started.data.config_hash
    : undefined;
}

/** The config_hash a stored thread was started with, read back through the log. */
export function pinOf(log: LogStore, threadId: ThreadId): string | undefined {
  const read = logOf(log, threadId);
  return read === undefined ? undefined : pinIn(read);
}

/**
 * Whether the schedule stays on its thread: it pins the same config, or it doesn't but the
 * thread is still busy. A config change moves to a new thread only once the old one is quiet (no
 * open turn, no undecided reservation), so no run is left behind where recovery won't look and
 * no new run starts while the old one goes on.
 */
function keeps(
  db: SqliteDriver,
  log: LogStore,
  threadId: ThreadId,
  started: EventDraft,
): boolean {
  const read = logOf(log, threadId);
  // An unreadable thread can't be shown quiet: the pass fails, and its identity stays.
  if (read === undefined)
    throw new Error(`schedule thread ${threadId} can't be read`);
  if (pinIn(read) === hashOf(started)) return true;
  return read.fold.turnOpen || pendingOf(db, log.tenant, threadId).length > 0;
}

function logOf(log: LogStore, threadId: ThreadId): VerifiedLog | undefined {
  const main = log.mainBranch(threadId);
  const read = main.ok ? log.read(main.value) : undefined;
  return read?.ok === true ? read.value : undefined;
}

function pinIn(read: VerifiedLog): string | undefined {
  const started = knownEvents(read).find((e) => e.type === "thread_started");
  return started?.type === "thread_started"
    ? started.data.config_hash
    : undefined;
}

function reserved(db: SqliteDriver, tenant: string, row: Due): boolean {
  return (
    db.all(
      `SELECT 1 FROM schedule_occurrences
        WHERE tenant_id = ? AND schedule_id = ? AND occurrence_at = ?`,
      [tenant, row.schedule_id, row.occurrence_at],
    ).length > 0
  );
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

/** A new thread for the schedule, in the caller's transaction: identity, branch, thread_started. */
function newThread(
  db: SqliteDriver,
  log: LogStore,
  scheduleId: string,
  started: EventDraft,
): ThreadId {
  const threadId = ThreadId.parse(uuidv7(log.now()));
  const branchId = BranchId.parse(uuidv7(log.now()));
  db.run(
    `INSERT INTO schedule_threads (tenant_id, schedule_id, thread_id, created_at)
      VALUES (?, ?, ?, ?) ON CONFLICT (tenant_id, schedule_id)
      DO UPDATE SET thread_id = excluded.thread_id, created_at = excluded.created_at`,
    [log.tenant, scheduleId, threadId, log.now()],
  );
  must(log.createBranch(threadId, branchId));
  const writer = must(log.acquire(branchId, `schedule-${uuidv7(log.now())}`));
  must(writer.append([started]));
  writer.release();
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
