import { z } from "zod";
import { BranchId, TeamId, type ThreadId } from "../log";
import { ok, type Result } from "../result";
import type { LogError } from "../verify/error";
import type { SqliteDriver } from "./driver";
import type { Opened } from "./started";
import { parseRows } from "./tables";

// The rows a deletion removes, once deletion.ts has decided the set may go. A row that fails its
// schema is an error, never a reason to skip a delete (it would orphan the rows it names).

/** Every team-keyed index table: a doomed lead's team takes all of its rows. */
export const TEAM_TABLES: readonly string[] = [
  "teams",
  "team_members",
  "mail",
  "asks",
  "monitors",
  "operator_receipts",
  "team_feed",
];

const BranchRow = z.strictObject({ branch_id: BranchId });

export function branchesOf(
  db: SqliteDriver,
  thread: string,
): Result<readonly BranchId[], LogError> {
  const rows = parseRows(
    BranchRow,
    db.all("SELECT branch_id FROM branches WHERE thread_id = ?", [thread]),
  );
  return rows.ok ? ok(rows.value.map((r) => r.branch_id)) : rows;
}

const TeamRow = z.strictObject({ team_id: TeamId, lead_thread_id: z.string() });

/**
 * The teams the set takes (Gate 1 §4.15 rule 2): those whose `teams.lead_thread_id` is doomed,
 * in this tenant, and those whose team log is doomed.
 */
export function doomedTeams(
  db: SqliteDriver,
  tenantId: string,
  opened: readonly Opened[],
  doomed: ReadonlySet<string>,
): Result<ReadonlySet<TeamId>, LogError> {
  const rows = parseRows(
    TeamRow,
    db.all("SELECT team_id, lead_thread_id FROM teams WHERE tenant_id = ?", [
      tenantId,
    ]),
  );
  if (!rows.ok) return rows;
  const teams = new Set<TeamId>(
    rows.value.flatMap((r) =>
      doomed.has(r.lead_thread_id) ? [r.team_id] : [],
    ),
  );
  for (const o of opened)
    if (o.event.type === "team_opened" && doomed.has(o.threadId))
      teams.add(o.event.data.team);
  return ok(teams);
}

/** Every index row of `team`: its teams row only in this tenant. */
export function deleteTeam(
  db: SqliteDriver,
  tenantId: string,
  team: TeamId,
): void {
  for (const table of TEAM_TABLES)
    db.run(
      table === "teams"
        ? "DELETE FROM teams WHERE team_id = ? AND tenant_id = ?"
        : `DELETE FROM ${table} WHERE team_id = ?`,
      table === "teams" ? [team, tenantId] : [team],
    );
}

/**
 * One thread's rows: its branches' log rows, leases, cursors and wake rows (and its parent's wake
 * row for it), approvals, inbox and channel rows, receipts and budget rows go; its live resources
 * move to releasing for gc; a tombstone records it.
 */
export function deleteOne(
  db: SqliteDriver,
  tenantId: string,
  threadId: ThreadId,
  now: number,
): Result<void, LogError> {
  const branches = branchesOf(db, threadId);
  if (!branches.ok) return branches;
  for (const branch of branches.value) {
    for (const table of [
      "events",
      "leases",
      "observer_cursors",
      "pending_wakes",
    ])
      db.run(`DELETE FROM ${table} WHERE branch_id = ?`, [branch]);
    db.run(
      "UPDATE resources SET state = 'releasing' WHERE owner_branch_id = ? AND state = 'live'",
      [branch],
    );
  }
  db.run("DELETE FROM pending_wakes WHERE child_thread_id = ?", [threadId]);
  for (const table of [
    "approvals",
    "inbox",
    "channel_threads",
    "run_receipts",
    "schedule_threads",
  ])
    db.run(`DELETE FROM ${table} WHERE thread_id = ? AND tenant_id = ?`, [
      threadId,
      tenantId,
    ]);
  // Its undecided reservations are dropped, never logged; the rows only keep their keys taken.
  db.run(
    `UPDATE schedule_occurrences SET state = 'retired'
      WHERE thread_id = ? AND tenant_id = ? AND state = 'pending'`,
    [threadId, tenantId],
  );
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
  return ok(undefined);
}
