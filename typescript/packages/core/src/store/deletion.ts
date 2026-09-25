import { z } from "zod";
import { BranchId, ThreadId } from "../log";
import { err, ok, type Result } from "../result";
import { verifyLines } from "../verify";
import { type LogError, logError } from "../verify/error";
import { branchesOf, deleteOne, deleteTeam, doomedTeams } from "./delete-rows";
import type { Sql, Tx } from "./driver";
import { branchLines } from "./lines";
import { type Opened, openedThreads } from "./started";
import { atomically, parseRows } from "./tables";

// `threads delete` (spec/schema/README.md, "Deleting a thread"; Gate 1 §4.15): the deletion set
// is a fixed point, deleted in one transaction, only when nothing in it can still run. Every
// check fails closed: what can't be read is never taken as safe. The resource ledger is never
// deleted with log rows: it owns cleanup.

export { TEAM_TABLES } from "./delete-rows";

/**
 * Deletes a thread of `tenantId` with everything that can't outlive it: its subagent and team
 * member threads, recursively, and the team log and index rows of every team a deleted thread
 * leads. Handoff targets are independent and stay. Returns how many threads were deleted:
 * not_found for another tenant's thread; thread_in_team for a member or team log whose lead
 * isn't deleted with it; busy while any of them could still run. A refusal writes nothing.
 */
export async function deleteThread(
  sql: Sql,
  tenantId: string,
  threadId: ThreadId,
  now: number,
): Promise<Result<number, LogError>> {
  return await atomically(sql, async (tx) => {
    const all = await threadsOf(tx, tenantId);
    if (!all.ok) return all;
    if (!all.value.includes(threadId))
      return err(logError("not_found", `no thread ${threadId}`));
    return await deleteSet(tx, tenantId, all.value, [threadId], now);
  });
}

/** Every thread of `tenantId`, as one deletion set in one transaction. */
export async function deleteTenant(
  sql: Sql,
  tenantId: string,
  now: number,
): Promise<Result<number, LogError>> {
  return await atomically(sql, async (tx) => {
    const all = await threadsOf(tx, tenantId);
    return all.ok
      ? await deleteSet(tx, tenantId, all.value, all.value, now)
      : all;
  });
}

const ThreadRow = z.strictObject({ thread_id: ThreadId });

async function threadsOf(
  tx: Tx,
  tenantId: string,
): Promise<Result<readonly ThreadId[], LogError>> {
  const rows = parseRows(
    ThreadRow,
    await tx.all("SELECT thread_id FROM threads WHERE tenant_id = ?", [
      tenantId,
    ]),
  );
  return rows.ok ? ok(rows.value.map((r) => r.thread_id)) : rows;
}

async function deleteSet(
  tx: Tx,
  tenantId: string,
  all: readonly ThreadId[],
  start: readonly ThreadId[],
  now: number,
): Promise<Result<number, LogError>> {
  const opened = await openedThreads(tx, tenantId);
  if (!opened.ok) return opened;
  const doomed = deletionSet(opened.value, start);
  const inTeam = outsideItsTeam(opened.value, doomed, new Set(all));
  if (inTeam !== undefined) return err(inTeam);
  const quiet = await running(tx, doomed, now);
  if (!quiet.ok) return quiet;
  const teams = await doomedTeams(tx, tenantId, doomed);
  if (!teams.ok) return teams;
  for (const team of teams.value) await deleteTeam(tx, tenantId, team);
  for (const thread of all.filter((t) => doomed.has(t))) {
    const gone = await deleteOne(tx, tenantId, thread, now);
    if (!gone.ok) return gone;
  }
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

/** A team member or team log goes only with its lead, unless that lead is already gone. */
function outsideItsTeam(
  opened: readonly Opened[],
  doomed: ReadonlySet<string>,
  existing: ReadonlySet<string>,
): LogError | undefined {
  for (const o of opened) {
    const lead = ownerOf(o);
    if (!doomed.has(o.threadId) || lead === undefined) continue;
    if (doomed.has(lead) || !existing.has(lead)) continue;
    const e = o.event;
    if (e.type === "team_opened")
      return inTeam(
        `thread ${o.threadId} is the team log of team ${e.data.team}`,
        lead,
      );
    if (e.data.parent?.relation === "team_member")
      return inTeam(`thread ${o.threadId} is a member of a team`, lead);
  }
  return undefined;
}

const inTeam = (what: string, lead: string): LogError =>
  logError(
    "thread_in_team",
    `${what}: delete its lead ${lead}, and the whole team goes with it`,
  );

const LeaseRow = z.strictObject({ branch_id: BranchId });

/**
 * busy when a branch in the set has an unexpired lease (a live executor), an effect in doubt
 * (begun or unknown), or a log that doesn't verify, which can't be proved free of one: deleting
 * it would erase the only record recovery settles an effect from.
 */
async function running(
  tx: Tx,
  doomed: ReadonlySet<string>,
  now: number,
): Promise<Result<void, LogError>> {
  for (const thread of doomed) {
    const leased = parseRows(
      LeaseRow,
      await tx.all(
        `SELECT l.branch_id FROM leases l JOIN branches b ON b.branch_id = l.branch_id
          WHERE b.thread_id = ? AND l.expires_at > ?`,
        [thread, now],
      ),
    );
    if (!leased.ok) return leased;
    const live = leased.value[0];
    if (live !== undefined)
      return err(busy(thread, `branch ${live.branch_id} holds a live lease`));
    const branches = await branchesOf(tx, thread);
    if (!branches.ok) return branches;
    for (const branch of branches.value) {
      const why = await unsettled(tx, branch);
      if (why !== undefined) return err(busy(thread, why));
    }
  }
  return ok(undefined);
}

/** Why `branch` may still hold an effect only its log can settle, if it may. */
async function unsettled(
  tx: Tx,
  branch: BranchId,
): Promise<string | undefined> {
  const lines = await branchLines(tx, branch);
  const log = lines.ok ? verifyLines(lines.value.lines) : lines;
  if (!log.ok)
    return `branch ${branch} doesn't verify (${log.error.code}): run \`threads repair ${branch}\` if its tail is torn, or delete it with the version that wrote it`;
  const doubt = [...log.value.fold.effects.values()].some(
    (effect) => effect.status === "begun" || effect.status === "unknown",
  );
  return doubt ? `branch ${branch} has an effect in doubt` : undefined;
}

const busy = (thread: string, why: string): LogError =>
  logError(
    "busy",
    `thread ${thread} can't be deleted yet (${why}): cancel it, wait for it to stop, resolve any parked effect, then delete`,
  );
