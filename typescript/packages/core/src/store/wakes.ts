import { z } from "zod";
import { BranchId, type KnownEvent, ThreadId } from "../log";
import type { Strict } from "../log/zod-types";
import type { ChainEvent } from "../verify";
import type { SqliteDriver } from "./driver";

// The pending_wakes index (spec/schema/store.sql; Gate 1 §2.7.3): one row per running background
// child of a branch, inserted in the append of its agent_spawned and deleted in the append of its
// agent_finished, or of the parent's parked{kind: child} for it (a parked child is resumed by the
// control path). A host resumes a branch with rows, so a child a crash stopped still reports and
// wakes its parent. The rows follow the replay rule: `pendingWakes` over a branch's own events is
// the fold that rebuilds them.

/** The row an event inserts or deletes, if any. */
function change(
  e: KnownEvent,
): { readonly add: boolean; readonly child: string } | undefined {
  if (e.type === "agent_spawned" && e.data.mode === "background")
    return { add: true, child: e.data.child_thread_id };
  if (e.type === "agent_finished")
    return { add: false, child: e.data.child_thread_id };
  if (e.type === "parked" && e.data.address.kind === "child")
    return { add: false, child: e.data.address.id };
  return undefined;
}

/** The background children of `branch` still waiting to report, in spawn order. */
export function pendingWakes(
  events: readonly KnownEvent[],
  branch: string,
): readonly string[] {
  const rows = new Set<string>();
  for (const e of events) {
    const row = e.branch_id === branch ? change(e) : undefined;
    if (row?.add === true) rows.add(row.child);
    else if (row !== undefined) rows.delete(row.child);
  }
  return [...rows];
}

/** Runs inside an append's transaction, after its events are inserted. */
export function recordWakes(
  db: SqliteDriver,
  branch: string,
  added: readonly ChainEvent[],
): void {
  for (const line of added) {
    const row = line.kind === "event" ? change(line.event) : undefined;
    if (row === undefined) continue;
    db.run(
      row.add
        ? "INSERT OR IGNORE INTO pending_wakes (branch_id, child_thread_id) VALUES (?, ?)"
        : "DELETE FROM pending_wakes WHERE branch_id = ? AND child_thread_id = ?",
      [branch, row.child],
    );
  }
}

const WakeBranch: Strict<{
  tenant_id: z.ZodString;
  thread_id: typeof ThreadId;
  branch_id: typeof BranchId;
}> = z.strictObject({
  tenant_id: z.string(),
  thread_id: ThreadId,
  branch_id: BranchId,
});
export type WakeBranch = z.infer<typeof WakeBranch>;

/** Every branch with a background child still to report: what a host resumes. */
export function wakeBranches(db: SqliteDriver): readonly WakeBranch[] {
  const rows = db.all(
    `SELECT DISTINCT b.tenant_id, b.thread_id, w.branch_id FROM pending_wakes w
      JOIN branches b ON b.branch_id = w.branch_id`,
    [],
  );
  return rows.flatMap((row) => {
    const parsed = WakeBranch.safeParse(row);
    return parsed.success ? [parsed.data] : [];
  });
}

/**
 * An index wipe's repair: every row again from the logs alone. `read` is each branch's
 * resolved events.
 */
export function rebuildWakes(
  db: SqliteDriver,
  read: (branch: string) => readonly KnownEvent[] | undefined,
): void {
  db.run("DELETE FROM pending_wakes", []);
  const branches = db.all("SELECT branch_id FROM branches", []);
  for (const row of branches) {
    const branch = z.object({ branch_id: z.string() }).safeParse(row);
    if (!branch.success) continue;
    const { branch_id } = branch.data;
    for (const child of pendingWakes(read(branch_id) ?? [], branch_id))
      db.run(
        "INSERT INTO pending_wakes (branch_id, child_thread_id) VALUES (?, ?)",
        [branch_id, child],
      );
  }
}
