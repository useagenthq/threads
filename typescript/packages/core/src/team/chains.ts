import { z } from "zod";
import {
  BranchId,
  type KnownEvent,
  type MailEnvelope,
  type TeamId,
} from "../log";
import { knownEvents } from "../reduce";
import { err, ok, type Result } from "../result";
import type { LogStore } from "../store";
import type { Tx } from "../store/driver";
import { type Opened, openedThreads } from "../store/started";
import { parseRows } from "../store/tables";
import type { VerifiedLog } from "../verify";
import { type LogError, logError } from "../verify/error";
import type { TeamLog } from "./scope";

// Which logs a team's index is folded from: a lead's team has its lead, its members and its team
// log; a host team (Teams Phase 2) is leadless, so its log is found by the team its team_opened
// names, its host members by that log's member_started{host_member} events, and its callers by a
// scan, since a caller's own log may be the only record of a pending ask.

/** One of the team's logs, verified. */
export type TeamChain = TeamLog & { readonly chain: VerifiedLog };

/** Code-unit order, as SQLite's BINARY collation and Python sort ids. */
export const byText = (a: string, b: string): number =>
  a < b ? -1 : a > b ? 1 : 0;

/**
 * The team's logs in `store`'s tenant, each main branch read verified, in branch order.
 * not_found when neither a lead nor a host team's log names the team.
 */
export async function chainsIn(
  tx: Tx,
  store: LogStore,
  teamId: TeamId,
): Promise<Result<readonly TeamChain[], LogError>> {
  const opened = await openedThreads(tx, store.tenant);
  if (!opened.ok) return opened;
  const found =
    hostTeam(opened.value, teamId) ?? leadTeam(opened.value, teamId);
  if (found === undefined)
    return err(logError("not_found", `no lead of team ${teamId}`));
  const chains: TeamChain[] = [];
  for (const o of found) {
    const chain = await store.readIn(tx, o.branchId);
    if (!chain.ok) return chain;
    chains.push({
      threadId: o.threadId,
      branchId: o.branchId,
      chain: chain.value,
    });
  }
  const callers = await callerChains(tx, store, teamId, chains);
  if (!callers.ok) return callers;
  return ok(
    [...chains, ...callers.value].toSorted((a, b) =>
      byText(a.branchId, b.branchId),
    ),
  );
}

/** A lead's team: the lead's log (its thread_started names the team), its members', the team log. */
function leadTeam(
  opened: readonly Opened[],
  teamId: TeamId,
): readonly Opened[] | undefined {
  const lead = opened.find(
    (o) =>
      o.event.type === "thread_started" && o.event.data.team?.id === teamId,
  );
  if (lead === undefined || lead.event.type !== "thread_started")
    return undefined;
  const logBranch = lead.event.data.team?.log_branch_id;
  const members = opened.filter(
    (o) =>
      o.event.type === "thread_started" &&
      o.event.data.parent?.relation === "team_member" &&
      o.event.data.parent.thread_id === lead.threadId,
  );
  const teamLog = opened.filter(
    (o) => o.event.type === "team_opened" && o.branchId === logBranch,
  );
  return [lead, ...members, ...teamLog];
}

/**
 * A host team: its log (the team_opened that names the team) and its host members, each found from
 * a member_started{host_member} naming its own root thread — a host member has no parent to walk.
 */
function hostTeam(
  opened: readonly Opened[],
  teamId: TeamId,
): readonly Opened[] | undefined {
  const log = opened.find(
    (o) =>
      o.event.type === "team_opened" &&
      o.event.data.kind === "host" &&
      o.event.data.team === teamId,
  );
  if (log === undefined) return undefined;
  const members = opened.filter(
    (o) =>
      o.event.type === "thread_started" &&
      o.event.data.host_member?.team === teamId,
  );
  return [log, ...members];
}

const BranchRow = z.strictObject({ branch_id: BranchId });

/**
 * The tenant's callers of this host team: a branch outside it whose log holds mail naming it with
 * a caller address. A caller's log may be the only record of a pending ask, so the rebuild reads
 * it too.
 *
 * ponytail: a full events scan by type, since rebuilds are rare (no new index).
 */
async function callerChains(
  tx: Tx,
  store: LogStore,
  teamId: TeamId,
  known: readonly TeamChain[],
): Promise<Result<readonly TeamChain[], LogError>> {
  const inTeam = new Set<string>(known.map((c) => c.branchId));
  const rows = parseRows(
    BranchRow,
    await tx.all(
      `SELECT DISTINCT b.branch_id FROM events e JOIN branches b ON b.branch_id = e.branch_id
        WHERE b.tenant_id = ? AND b.parent_branch_id IS NULL
          AND e.type IN ('message_sent', 'message_received', 'mail_refused', 'ask_closed')
        ORDER BY b.branch_id`,
      [store.tenant],
    ),
  );
  if (!rows.ok) return rows;
  const out: TeamChain[] = [];
  for (const { branch_id } of rows.value) {
    if (inTeam.has(branch_id)) continue;
    const chain = await store.readIn(tx, branch_id);
    if (!chain.ok) return chain;
    const events = knownEvents(chain.value);
    if (!events.some((e) => callerMail(e, teamId))) continue;
    const first = events[0];
    if (first === undefined) continue;
    out.push({
      threadId: first.thread_id,
      branchId: first.branch_id,
      chain: chain.value,
    });
  }
  return ok(out);
}

/** An event whose envelope names `teamId` with a caller address at either end. */
function callerMail(e: KnownEvent, teamId: TeamId): boolean {
  if (e.type !== "message_sent" && e.type !== "message_received") return false;
  const env = e.data.envelope;
  return env.team === teamId && (namesCaller(env.from) || namesCaller(env.to));
}

const namesCaller = (
  address: MailEnvelope["from"] | MailEnvelope["to"],
): boolean => typeof address === "object" && "caller" in address;

/**
 * The caller threads this tenant has tombstoned: a mail or ask naming one is not rebuilt, so the
 * wiped-and-rebuilt index equals the index the delete left (`team_index.py`'s `deleted`).
 */
export async function deletedThreads(
  tx: Tx,
  tenant: string,
): Promise<Result<ReadonlySet<string>, LogError>> {
  const rows = parseRows(
    z.strictObject({ thread_id: z.string() }),
    await tx.all("SELECT thread_id FROM tombstones WHERE tenant_id = ?", [
      tenant,
    ]),
  );
  return rows.ok ? ok(new Set(rows.value.map((r) => r.thread_id))) : rows;
}

/** Whether a mail naming a deleted caller thread: its row is never rebuilt. */
export function namesDeletedCaller(
  e: KnownEvent,
  deleted: ReadonlySet<string>,
): boolean {
  if (e.type !== "message_sent" || deleted.size === 0) return false;
  const { from, to } = e.data.envelope;
  return [from, to].some(
    (a) =>
      typeof a === "object" && "caller" in a && deleted.has(a.caller.thread_id),
  );
}
