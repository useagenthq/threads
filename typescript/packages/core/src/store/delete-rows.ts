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

/** Only a host team has callers, so only its rows name a branch outside every team. */
const HOST_TEAMS = "SELECT team_id FROM teams WHERE kind = 'host'";

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

const TeamRow = z.strictObject({
  team_id: TeamId,
  lead_thread_id: z.string().nullable(),
});

/**
 * The teams the set takes (Gate 1 §4.15 rule 2): those whose `teams.lead_thread_id` is doomed,
 * in this tenant. A team id is never taken from a log: an imported team_opened could name
 * another tenant's team. A host team is leadless and never closes, so nothing takes it.
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
        r.lead_thread_id !== null && doomed.has(r.lead_thread_id)
          ? [r.team_id]
          : [],
      ),
    ),
  );
}

/**
 * Why a caller branch can still run (Teams Phase 2, R29-1): it is the asker of an open ask, the
 * recipient of an unconsumed reply or bounce, or the sender of a mail no host member has consumed.
 * A MailId is `<sender branch_id>:<call or event id>`, so a prefix names the branch's own sends;
 * `mail` has no sender column.
 */
export async function callerHolds(
  tx: Tx,
  branch: BranchId,
): Promise<string | undefined> {
  const found = await tx.all(
    `SELECT 1 FROM asks WHERE team_id IN (${HOST_TEAMS}) AND asker_branch_id = ? AND state = 'open'
      UNION ALL SELECT 1 FROM mail WHERE team_id IN (${HOST_TEAMS}) AND state = 'pending'
        AND (to_branch_id = ? OR mail_id LIKE ?) LIMIT 1`,
    [branch, branch, `${branch}:%`],
  );
  return found.length > 0
    ? `branch ${branch} has an open ask, an unconsumed answer or a mail a host member has not taken`
    : undefined;
}

/**
 * Every host-team `mail` and `asks` row naming a doomed branch as caller: its sends, the answers
 * addressed to it, and the asks it opened. A rebuild skips them too, so the wiped-and-rebuilt
 * index equals the index this delete left.
 */
export async function deleteCallerRows(
  tx: Tx,
  branch: BranchId,
): Promise<void> {
  await tx.run(
    `DELETE FROM mail WHERE team_id IN (${HOST_TEAMS})
      AND (to_branch_id = ? OR mail_id LIKE ?)`,
    [branch, `${branch}:%`],
  );
  await tx.run(
    `DELETE FROM asks WHERE team_id IN (${HOST_TEAMS}) AND asker_branch_id = ?`,
    [branch],
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
