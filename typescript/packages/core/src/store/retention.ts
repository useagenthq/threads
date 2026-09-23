import { readdirSync, rmSync, statSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { err, ok, type Result } from "../result";
import { type LogError, logError } from "../verify/error";
import type { SqliteDriver } from "./driver";
import { atomically, parseRows } from "./tables";

// Retention: `threads delete` and the artifact half of `threads gc`. Nothing is
// deleted automatically. The resource ledger is never deleted with log rows: it owns cleanup.

const Id = z.strictObject({ id: z.string() });

function ids(
  db: SqliteDriver,
  sql: string,
  params: readonly string[],
): string[] {
  const rows = parseRows(Id, db.all(sql, params));
  return rows.ok ? rows.value.map((r) => r.id) : [];
}

/**
 * Deletes a thread of `tenantId` and every subagent thread spawned under it, recursively, in one
 * transaction (spec/schema/README.md, "Deleting a thread"). Handoff targets are independent
 * threads and stay. Another tenant's thread is not_found.
 */
export function deleteThread(
  db: SqliteDriver,
  tenantId: string,
  threadId: string,
  now: number,
): Result<void, LogError> {
  return atomically(db, () => {
    const owned = ids(
      db,
      "SELECT thread_id AS id FROM threads WHERE thread_id = ? AND tenant_id = ?",
      [threadId, tenantId],
    );
    if (owned.length === 0)
      return err(logError("not_found", `no thread ${threadId}`));
    const doomed = [threadId];
    for (const id of doomed) doomed.push(...subagentsOf(db, tenantId, id));
    for (const id of doomed) deleteOne(db, tenantId, id, now);
    return ok(undefined);
  });
}

const Started = z.object({
  data: z.object({
    parent: z
      .object({ relation: z.string(), thread_id: z.string() })
      .optional(),
  }),
});
const StartedRow = z.strictObject({
  id: z.string(),
  line: z.instanceof(Uint8Array),
});

/** The tenant's threads whose thread_started names `parentId` as their subagent parent. */
function subagentsOf(
  db: SqliteDriver,
  tenantId: string,
  parentId: string,
): readonly string[] {
  const rows = parseRows(
    StartedRow,
    db.all(
      `SELECT b.thread_id AS id, e.line FROM events e
        JOIN branches b ON b.branch_id = e.branch_id
        JOIN threads t ON t.thread_id = b.thread_id
        WHERE t.tenant_id = ? AND e.type = 'thread_started' AND e.line LIKE ?`,
      [tenantId, `%${parentId}%`],
    ),
  );
  if (!rows.ok) return [];
  return rows.value.flatMap((row) => {
    const started = Started.safeParse(
      JSON.parse(new TextDecoder().decode(row.line)),
    );
    const parent = started.success ? started.data.data.parent : undefined;
    return parent?.relation === "subagent" && parent.thread_id === parentId
      ? [row.id]
      : [];
  });
}

/**
 * One thread's rows: its branches' log rows, leases, cursors, approvals, inbox and channel rows,
 * receipts and budget rows go; its live resources move to releasing for gc; a tombstone records
 * it.
 */
function deleteOne(
  db: SqliteDriver,
  tenantId: string,
  threadId: string,
  now: number,
): void {
  const branches = ids(
    db,
    "SELECT branch_id AS id FROM branches WHERE thread_id = ?",
    [threadId],
  );
  for (const branch of branches) {
    db.run("DELETE FROM events WHERE branch_id = ?", [branch]);
    db.run("DELETE FROM leases WHERE branch_id = ?", [branch]);
    db.run("DELETE FROM observer_cursors WHERE branch_id = ?", [branch]);
    db.run(
      "UPDATE resources SET state = 'releasing' WHERE owner_branch_id = ? AND state = 'live'",
      [branch],
    );
  }
  for (const table of ["approvals", "inbox", "channel_threads", "run_receipts"])
    db.run(`DELETE FROM ${table} WHERE thread_id = ? AND tenant_id = ?`, [
      threadId,
      tenantId,
    ]);
  db.run("DELETE FROM budget_ledger WHERE budget_id = ? OR budget_id LIKE ?", [
    `thread:${threadId}`,
    `run:${threadId}:%`,
  ]);
  db.run("DELETE FROM branches WHERE thread_id = ?", [threadId]);
  db.run("DELETE FROM threads WHERE thread_id = ?", [threadId]);
  db.run(
    "INSERT INTO tombstones (thread_id, tenant_id, deleted_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
    [threadId, tenantId, now],
  );
}

/** Every thread of `tenantId`, deleted as deleteThread does. */
export function deleteTenant(
  db: SqliteDriver,
  tenantId: string,
  now: number,
): Result<number, LogError> {
  const threads = ids(
    db,
    "SELECT thread_id AS id FROM threads WHERE tenant_id = ?",
    [tenantId],
  );
  for (const thread of threads) {
    const done = deleteThread(db, tenantId, thread, now);
    // A subagent thread already went with its parent.
    if (!done.ok && done.error.code !== "not_found") return done;
  }
  return ok(threads.length);
}

const SHA = /"sha256":"([0-9a-f]{64})"/g;
const Line = z.strictObject({ line: z.instanceof(Uint8Array) });

/**
 * The artifact sweep of `threads gc`: every artifact some stored line names (events, headers,
 * a torn import's dropped bytes) is kept; any other older than `olderThan` is removed. Returns
 * the removed hashes.
 */
export function sweepArtifacts(
  db: SqliteDriver,
  root: string,
  olderThan: number,
): readonly string[] {
  const kept = referenced(db);
  const removed: string[] = [];
  const base = join(root, "sha256");
  for (const prefix of safeList(base))
    for (const name of safeList(join(base, prefix))) {
      const file = join(base, prefix, name);
      if (kept.has(name) || statSync(file).mtimeMs >= olderThan) continue;
      rmSync(file);
      removed.push(name);
    }
  return removed;
}

/** Every artifact hash a stored line or a torn import's dropped bytes name. */
function referenced(db: SqliteDriver): ReadonlySet<string> {
  const kept = new Set<string>();
  const decoder = new TextDecoder();
  const lines = parseRows(Line, db.all("SELECT line FROM events", []));
  for (const { line } of lines.ok ? lines.value : [])
    for (const m of decoder.decode(line).matchAll(SHA))
      if (m[1] !== undefined) kept.add(m[1]);
  const dropped = ids(
    db,
    "SELECT dropped_ref AS id FROM branches WHERE dropped_ref IS NOT NULL",
    [],
  );
  for (const id of dropped) kept.add(id);
  return kept;
}

function safeList(dir: string): readonly string[] {
  try {
    return readdirSync(dir);
  } catch {
    return [];
  }
}
