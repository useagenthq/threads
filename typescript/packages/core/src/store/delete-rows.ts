import { z } from "zod";
import { BranchId, TeamId, type ThreadId } from "../log";
import { ok, type Result } from "../result";
import type { LogError } from "../verify/error";
import type { Tx } from "./driver";
import { recordLosses } from "./losses";
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

export async function branchesOf(
  tx: Tx,
  thread: string,
): Promise<Result<readonly BranchId[], LogError>> {
  const rows = parseRows(
    BranchRow,
    await tx.all("SELECT branch_id FROM branches WHERE thread_id = ?", [
      thread,
    ]),
  );
  return rows.ok ? ok(rows.value.map((r) => r.branch_id)) : rows;
}

const TeamRow = z.strictObject({ team_id: TeamId, lead_thread_id: z.string() });

/**
 * The teams the set takes (Gate 1 §4.15 rule 2): those whose `teams.lead_thread_id` is doomed,
 * in this tenant. A team id is never taken from a log: an imported team_opened could name
 * another tenant's team.
 */
export async function doomedTeams(
  tx: Tx,
  tenantId: string,
  doomed: ReadonlySet<string>,
): Promise<Result<ReadonlySet<TeamId>, LogError>> {
  const rows = parseRows(
    TeamRow,
    await tx.all(
      "SELECT team_id, lead_thread_id FROM teams WHERE tenant_id = ?",
      [tenantId],
    ),
  );
  if (!rows.ok) return rows;
  return ok(
    new Set(
      rows.value.flatMap((r) =>
        doomed.has(r.lead_thread_id) ? [r.team_id] : [],
      ),
    ),
  );
}

/** Every index row of `team`: its teams row only in this tenant. */
export async function deleteTeam(
  tx: Tx,
  tenantId: string,
  team: TeamId,
): Promise<void> {
  for (const table of TEAM_TABLES)
    await tx.run(
      table === "teams"
        ? "DELETE FROM teams WHERE team_id = ? AND tenant_id = ?"
        : `DELETE FROM ${table} WHERE team_id = ?`,
      table === "teams" ? [team, tenantId] : [team],
    );
}

/**
 * One thread's rows: its branches' log rows, leases, cursors, wake and question rows (and its
 * parent's wake row for it), approvals, inbox and channel rows, receipts and budget rows go; its
 * live resources move to releasing for gc; a tombstone and one loss row per telemetry observer
 * record it.
 */
export async function deleteOne(
  tx: Tx,
  tenantId: string,
  threadId: ThreadId,
  now: number,
): Promise<Result<void, LogError>> {
  const branches = await branchesOf(tx, threadId);
  if (!branches.ok) return branches;
  // Before the events go: what each telemetry exporter may not have sent yet.
  await recordLosses(tx, tenantId, threadId, now);
  for (const branch of branches.value) {
    for (const table of [
      "events",
      "leases",
      "observer_cursors",
      "pending_wakes",
      "questions",
    ])
      await tx.run(`DELETE FROM ${table} WHERE branch_id = ?`, [branch]);
    await tx.run(
      "UPDATE resources SET state = 'releasing' WHERE owner_branch_id = ? AND state = 'live'",
      [branch],
    );
  }
  await tx.run("DELETE FROM pending_wakes WHERE child_thread_id = ?", [
    threadId,
  ]);
  for (const table of [
    "approvals",
    "inbox",
    "channel_threads",
    "run_receipts",
    "schedule_threads",
  ])
    await tx.run(`DELETE FROM ${table} WHERE thread_id = ? AND tenant_id = ?`, [
      threadId,
      tenantId,
    ]);
  // Its undecided reservations are dropped, never logged; the rows only keep their keys taken.
  await tx.run(
    `UPDATE schedule_occurrences SET state = 'retired'
      WHERE thread_id = ? AND tenant_id = ? AND state = 'pending'`,
    [threadId, tenantId],
  );
  await tx.run(
    "DELETE FROM budget_ledger WHERE budget_id = ? OR budget_id LIKE ?",
    [`thread:${threadId}`, `run:${threadId}:%`],
  );
  await tx.run("DELETE FROM branches WHERE thread_id = ?", [threadId]);
  await tx.run("DELETE FROM threads WHERE thread_id = ?", [threadId]);
  await tx.run(
    "INSERT INTO tombstones (thread_id, tenant_id, deleted_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
    [threadId, tenantId, now],
  );
  return ok(undefined);
}
