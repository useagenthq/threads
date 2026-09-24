import type { KnownEvent } from "../log";
import { ok } from "../result";
import type { SqliteDriver } from "./driver";
import type { IndexHook } from "./indexing";
import { parseRows } from "./tables";
import { WakeBranch } from "./wakes";

// The questions table (store.sql): a projection of the log for the expiry scan. A row opens with
// the append of parked{awaiting_input} and is decided by the append of the call's settling
// result, in the same transactions. The log decides whether a question is open; a missing row
// blocks nothing. Import derives the rows from the same events, dated by the events themselves.

/**
 * Opens a row for each question park in `events` and decides the row of each settled call:
 * answered by an answer, expired by any other settling result (the expiry, a cancel).
 */
export function projectQuestions(
  db: SqliteDriver,
  row: { readonly tenant: string; readonly branch: string },
  events: readonly KnownEvent[],
  decidedAt: (e: KnownEvent) => number,
): void {
  for (const e of events) {
    if (
      e.type === "parked" &&
      e.data.reason === "awaiting_input" &&
      e.data.address.kind === "input" &&
      e.data.expires_at !== undefined
    )
      db.run(
        `INSERT INTO questions (tenant_id, branch_id, call_id, expires_at, state, decided_at)
          VALUES (?, ?, ?, ?, 'open', NULL) ON CONFLICT DO NOTHING`,
        [row.tenant, row.branch, e.data.address.id, e.data.expires_at],
      );
    if (e.type === "tool_result")
      db.run(
        `UPDATE questions SET state = ?, decided_at = ?
          WHERE branch_id = ? AND call_id = ? AND state = 'open'`,
        [
          e.data.origin === "answered" ? "answered" : "expired",
          decidedAt(e),
          row.branch,
          e.data.call_id,
        ],
      );
  }
}

/** The index hook: each append's question rows, decided at the append's clock. */
export const questionRows: IndexHook = (a) => {
  projectQuestions(
    a.db,
    { tenant: a.tenant, branch: a.branchId },
    a.events,
    () => a.now,
  );
  return ok(undefined);
};

/**
 * Every branch with an open question past its expires_at: what a host's tick resumes, so the run
 * records "no answer" (the question rows survive restarts, so one that expired while no host ran
 * closes on the first tick).
 */
export function dueQuestionBranches(
  db: SqliteDriver,
  now: number,
): readonly WakeBranch[] {
  const rows = parseRows(
    WakeBranch,
    db.all(
      `SELECT DISTINCT b.tenant_id, b.thread_id, q.branch_id FROM questions q
        JOIN branches b ON b.branch_id = q.branch_id
        WHERE q.state = 'open' AND q.expires_at <= ?`,
      [now],
    ),
  );
  return rows.ok ? rows.value : [];
}
