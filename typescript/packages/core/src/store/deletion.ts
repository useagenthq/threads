import { z } from "zod";
import { BranchId, ThreadId } from "../log";
import { err, ok, type Result } from "../result";
import { verifyLines } from "../verify";
import { type LogError, logError } from "../verify/error";
import type { SqliteDriver } from "./driver";
import { branchLines } from "./lines";
import { type Opened, openedThreads } from "./started";
import { atomically, parseRows } from "./tables";

// `threads delete` (spec/schema/README.md, "Deleting a thread"; Gate 1 §4.15): the deletion set
// is a fixed point, deleted in one transaction, only when nothing in it is still running. The
// resource ledger is never deleted with log rows: it owns cleanup.

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

/**
 * Deletes a thread of `tenantId` with everything that can't outlive it: its subagent and team
 * member threads, recursively, and the team log and index rows of every team a deleted thread
 * leads. Handoff targets are independent and stay. Returns how many threads were deleted:
 * not_found for another tenant's thread; thread_in_team for a member or team log whose lead
 * isn't deleted with it; busy while any of them runs. A refusal writes nothing.
 */
export function deleteThread(
  db: SqliteDriver,
  tenantId: string,
  threadId: string,
  now: number,
): Result<number, LogError> {
  return atomically(db, () => {
    const owned = threadsOf(db, tenantId, threadId);
    if (!owned.ok) return owned;
    if (owned.value.length === 0)
      return err(logError("not_found", `no thread ${threadId}`));
    return deleteSet(db, tenantId, owned.value, now);
  });
}

/** Every thread of `tenantId`, as one deletion set in one transaction. */
export function deleteTenant(
  db: SqliteDriver,
  tenantId: string,
  now: number,
): Result<number, LogError> {
  return atomically(db, () => {
    const all = threadsOf(db, tenantId);
    return all.ok ? deleteSet(db, tenantId, all.value, now) : all;
  });
}

const ThreadRow = z.strictObject({ thread_id: ThreadId });

/** The tenant's threads, or just `threadId` when the tenant owns it. */
function threadsOf(
  db: SqliteDriver,
  tenantId: string,
  threadId?: string,
): Result<readonly ThreadId[], LogError> {
  const rows = parseRows(
    ThreadRow,
    db.all(
      "SELECT thread_id FROM threads WHERE tenant_id = ? AND (? IS NULL OR thread_id = ?)",
      [tenantId, threadId ?? null, threadId ?? null],
    ),
  );
  return rows.ok ? ok(rows.value.map((r) => r.thread_id)) : rows;
}

function deleteSet(
  db: SqliteDriver,
  tenantId: string,
  start: readonly ThreadId[],
  now: number,
): Result<number, LogError> {
  const opened = openedThreads(db, tenantId);
  if (!opened.ok) return opened;
  const doomed = deletionSet(opened.value, start);
  const refused =
    outsideItsTeam(opened.value, doomed) ?? running(db, doomed, now);
  if (refused !== undefined) return err(refused);
  for (const o of opened.value) {
    const team =
      o.event.type === "thread_started" ? o.event.data.team : undefined;
    if (team !== undefined && doomed.has(o.threadId))
      for (const table of TEAM_TABLES)
        db.run(`DELETE FROM ${table} WHERE team_id = ?`, [team.id]);
  }
  for (const thread of doomed) deleteOne(db, tenantId, thread, now);
  return ok(doomed.size);
}

/**
 * The fixed point: add every thread whose parent (relation subagent or team_member) is in the
 * set, and the team log of every lead in the set, until nothing is added.
 */
function deletionSet(
  opened: readonly Opened[],
  start: readonly ThreadId[],
): ReadonlySet<string> {
  const set = new Set<string>(start);
  let grew = true;
  while (grew) {
    grew = false;
    for (const o of opened)
      if (!set.has(o.threadId) && belongsTo(o, set)) {
        set.add(o.threadId);
        grew = true;
      }
  }
  return set;
}

/** The thread that `o` can't outlive, if any: a child's parent, a team log's lead. */
function ownerOf(o: Opened): string | undefined {
  if (o.event.type === "team_opened") return o.event.data.lead_thread_id;
  const parent = o.event.data.parent;
  return parent?.relation === "subagent" || parent?.relation === "team_member"
    ? parent.thread_id
    : undefined;
}

const belongsTo = (o: Opened, set: ReadonlySet<string>): boolean => {
  const owner = ownerOf(o);
  return owner !== undefined && set.has(owner);
};

/** A team member or team log goes only with its lead. */
function outsideItsTeam(
  opened: readonly Opened[],
  doomed: ReadonlySet<string>,
): LogError | undefined {
  for (const o of opened) {
    if (!doomed.has(o.threadId) || belongsTo(o, doomed)) continue;
    const e = o.event;
    if (e.type === "team_opened")
      return inTeam(
        `thread ${o.threadId} is the team log of team ${e.data.team}`,
        e.data.lead_thread_id,
      );
    if (e.data.parent?.relation === "team_member")
      return inTeam(
        `thread ${o.threadId} is a member of a team`,
        e.data.parent.thread_id,
      );
  }
  return undefined;
}

const inTeam = (what: string, lead: string): LogError =>
  logError(
    "thread_in_team",
    `${what}: delete its lead ${lead}, and the whole team goes with it`,
  );

const BranchRow = z.strictObject({ branch_id: BranchId });

/**
 * busy when a branch in the set has an unexpired lease (a live executor) or an effect in doubt
 * (begun or unknown): deleting its log would erase the only record recovery settles it from. A
 * branch whose log no longer verifies can't be resumed by anyone, so only its lease counts.
 */
function running(
  db: SqliteDriver,
  doomed: ReadonlySet<string>,
  now: number,
): LogError | undefined {
  for (const thread of doomed) {
    const leased = parseRows(
      BranchRow,
      db.all(
        `SELECT l.branch_id FROM leases l JOIN branches b ON b.branch_id = l.branch_id
          WHERE b.thread_id = ? AND l.expires_at > ?`,
        [thread, now],
      ),
    );
    const live = leased.ok ? leased.value[0] : undefined;
    if (live !== undefined)
      return busy(thread, `branch ${live.branch_id} holds a live lease`);
    const doubt = branchesOf(db, thread).find((b) => inDoubt(db, b));
    if (doubt !== undefined)
      return busy(thread, `branch ${doubt} has an effect in doubt`);
  }
  return undefined;
}

const busy = (thread: string, why: string): LogError =>
  logError(
    "busy",
    `thread ${thread} is still running (${why}): cancel it, wait for it to stop, resolve any parked effect, then delete`,
  );

function branchesOf(db: SqliteDriver, thread: string): readonly BranchId[] {
  const rows = parseRows(
    BranchRow,
    db.all("SELECT branch_id FROM branches WHERE thread_id = ?", [thread]),
  );
  return rows.ok ? rows.value.map((r) => r.branch_id) : [];
}

function inDoubt(db: SqliteDriver, branch: BranchId): boolean {
  const lines = branchLines(db, branch);
  const log = lines.ok ? verifyLines(lines.value.lines) : undefined;
  return (
    log?.ok === true &&
    [...log.value.fold.effects.values()].some(
      (effect) => effect.status === "begun" || effect.status === "unknown",
    )
  );
}

/**
 * One thread's rows: its branches' log rows, leases, cursors and wake rows, approvals, inbox and
 * channel rows, receipts and budget rows go; its live resources move to releasing for gc; a
 * tombstone records it.
 */
function deleteOne(
  db: SqliteDriver,
  tenantId: string,
  threadId: string,
  now: number,
): void {
  for (const branch of branchesOf(db, threadId)) {
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
}
