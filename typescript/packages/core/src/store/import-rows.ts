import { z } from "zod";
import type { KnownEvent } from "../log";
import { ThreadId } from "../log";
import { err, ok, type Result } from "../result";
import { type LogError, logError } from "../verify/error";
import type { Tx } from "./driver";
import { parseRows } from "./tables";

// The host rows an import claims besides its events: a deleted thread's tombstone, which refuses
// the import, and a schedule thread's identity and occurrences, which it rebuilds from the
// chain's own schedule events (spec/schema/README.md, "Portable bundles").

/** The first of `threadIds` this tenant has deleted, if any: a tombstone refuses an import. */
export async function tombstoned(
  tx: Tx,
  tenantId: string,
  threadIds: readonly ThreadId[],
): Promise<Result<ThreadId | undefined, LogError>> {
  for (const threadId of threadIds) {
    const rows = parseRows(
      z.strictObject({ thread_id: ThreadId }),
      await tx.all(
        "SELECT thread_id FROM tombstones WHERE thread_id = ? AND tenant_id = ?",
        [threadId, tenantId],
      ),
    );
    if (!rows.ok) return rows;
    if (rows.value[0] !== undefined) return ok(threadId);
  }
  return ok(undefined);
}

export function deleted(threadId: ThreadId): LogError {
  return logError(
    "branch_exists",
    `thread ${threadId} was deleted from this store`,
  );
}

/** One decided occurrence, as its logged event records it. */
type Occurrence = {
  readonly schedule_id: string;
  readonly occurrence_at: number;
  readonly thread_id: ThreadId;
  readonly state: "fired" | "skipped";
  readonly reason: string | null;
  readonly logged_seq: number;
};

/**
 * The schedule rows a chain implies. Every occurrence it carries is decided, so the frozen
 * `agent`, `input_json` and `timezone` a pending row needs stay null.
 */
export function scheduleRows(
  events: readonly KnownEvent[],
): readonly Occurrence[] {
  return events.flatMap((e) =>
    e.type === "schedule_fired" || e.type === "schedule_skipped"
      ? [
          {
            schedule_id: e.data.schedule_id,
            occurrence_at: e.data.scheduled_for,
            thread_id: e.thread_id,
            state:
              e.type === "schedule_fired"
                ? ("fired" as const)
                : ("skipped" as const),
            reason: e.type === "schedule_skipped" ? e.data.reason : null,
            logged_seq: e.seq,
          },
        ]
      : [],
  );
}

/**
 * Claims the chain's schedule identity and occurrence keys inside the import's row transaction.
 * Each key is inserted with ON CONFLICT DO NOTHING and read back: one that now belongs to
 * another thread, or holds another occurrence, was claimed by a scheduler or a second importer
 * after prevalidation, so the whole import rolls back with `schedule_conflict`.
 */
export async function claimSchedules(
  tx: Tx,
  tenantId: string,
  events: readonly KnownEvent[],
  now: number,
): Promise<Result<void, LogError>> {
  const occurrences = scheduleRows(events);
  for (const schedule of new Map(
    occurrences.map((o) => [o.schedule_id, o.thread_id]),
  )) {
    const claimed = await claimIdentity(
      tx,
      tenantId,
      schedule[0],
      schedule[1],
      now,
    );
    if (!claimed.ok) return claimed;
  }
  for (const row of occurrences) {
    const claimed = await claimOccurrence(tx, tenantId, row, now);
    if (!claimed.ok) return claimed;
  }
  return ok(undefined);
}

async function claimIdentity(
  tx: Tx,
  tenantId: string,
  scheduleId: string,
  threadId: ThreadId,
  now: number,
): Promise<Result<void, LogError>> {
  await tx.run(
    `INSERT INTO schedule_threads (tenant_id, schedule_id, thread_id, current, created_at)
      VALUES (?, ?, ?, 1, ?) ON CONFLICT DO NOTHING`,
    [tenantId, scheduleId, threadId, now],
  );
  const current = parseRows(
    z.strictObject({ thread_id: ThreadId }),
    await tx.all(
      `SELECT thread_id FROM schedule_threads
        WHERE tenant_id = ? AND schedule_id = ? AND current = 1`,
      [tenantId, scheduleId],
    ),
  );
  if (!current.ok) return current;
  const held = current.value[0]?.thread_id;
  return held === undefined || held === threadId
    ? ok(undefined)
    : err(conflict(`schedule ${scheduleId} runs on another thread here`));
}

const StoredOccurrence = z.strictObject({
  thread_id: ThreadId,
  state: z.string(),
  reason: z.string().nullable(),
});

async function claimOccurrence(
  tx: Tx,
  tenantId: string,
  row: Occurrence,
  now: number,
): Promise<Result<void, LogError>> {
  await tx.run(
    `INSERT INTO schedule_occurrences (tenant_id, schedule_id, occurrence_at, state, reason,
      thread_id, claimed_at, logged_seq)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING`,
    [
      tenantId,
      row.schedule_id,
      row.occurrence_at,
      row.state,
      row.reason,
      row.thread_id,
      now,
      row.logged_seq,
    ],
  );
  const stored = parseRows(
    StoredOccurrence,
    await tx.all(
      `SELECT thread_id, state, reason FROM schedule_occurrences
        WHERE tenant_id = ? AND schedule_id = ? AND occurrence_at = ?`,
      [tenantId, row.schedule_id, row.occurrence_at],
    ),
  );
  if (!stored.ok) return stored;
  const held = stored.value[0];
  const same =
    held !== undefined &&
    held.thread_id === row.thread_id &&
    held.state === row.state &&
    held.reason === row.reason;
  return same
    ? ok(undefined)
    : err(
        conflict(
          `occurrence ${row.schedule_id}@${row.occurrence_at} is decided otherwise here`,
        ),
      );
}

function conflict(why: string): LogError {
  return logError("schedule_conflict", why);
}
