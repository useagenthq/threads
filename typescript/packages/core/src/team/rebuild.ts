import { z } from "zod";
import type { TeamId } from "../log";
import { knownEvents } from "../reduce";
import { err, ok, type Result } from "../result";
import type { LogStore } from "../store";
import { TEAM_TABLES } from "../store/deletion";
import { READ_ONLY, type Tx } from "../store/driver";
import { openedThreads } from "../store/started";
import { atomically, parseRows } from "../store/tables";
import { refoldWakes } from "../store/wakes";
import type { VerifiedLog } from "../verify";
import { type LogError, logError } from "../verify/error";
import { checkTeamLogs } from "./cross";
import { changeRows, insertRows, type TeamLog, turnOpeners } from "./index";

// The replay rule's other half: wipe a team's index rows and fold them again from its logs
// alone, with the same insertRows/changeRows a writer uses, so a rebuild equals what the appends
// wrote (mail's claim columns aside, and team_feed, which starts a new epoch).

/** One of the team's logs, verified. */
export type TeamChain = TeamLog & { readonly chain: VerifiedLog };

/**
 * The team's logs in `store`'s tenant, each main branch read verified, in branch order: the
 * lead's (its thread_started names the team), its members' (parent relation team_member naming
 * the lead's thread) and the team log. not_found when no lead names the team.
 */
export function teamChains(
  store: LogStore,
  teamId: TeamId,
): Promise<Result<readonly TeamChain[], LogError>> {
  return store.driver.transaction(
    (tx) => chainsIn(tx, store, teamId),
    READ_ONLY,
  );
}

async function chainsIn(
  tx: Tx,
  store: LogStore,
  teamId: TeamId,
): Promise<Result<readonly TeamChain[], LogError>> {
  const opened = await openedThreads(tx, store.tenant);
  if (!opened.ok) return opened;
  const lead = opened.value.find(
    (o) =>
      o.event.type === "thread_started" && o.event.data.team?.id === teamId,
  );
  if (lead === undefined || lead.event.type !== "thread_started")
    return err(logError("not_found", `no lead of team ${teamId}`));
  const logBranch = lead.event.data.team?.log_branch_id;
  const members = opened.value.filter(
    (o) =>
      o.event.type === "thread_started" &&
      o.event.data.parent?.relation === "team_member" &&
      o.event.data.parent.thread_id === lead.threadId,
  );
  const teamLog = opened.value.filter(
    (o) => o.event.type === "team_opened" && o.branchId === logBranch,
  );
  const chains: TeamChain[] = [];
  for (const o of [lead, ...members, ...teamLog]) {
    const chain = await store.readIn(tx, o.branchId);
    if (!chain.ok) return chain;
    chains.push({
      threadId: o.threadId,
      branchId: o.branchId,
      chain: chain.value,
    });
  }
  return ok(chains.toSorted((a, b) => byText(a.branchId, b.branchId)));
}

/**
 * In one IMMEDIATE transaction, so no append lands between the read and the refold: reads the
 * team's logs, checks rule 43 across them, wipes every index row of the team (and the wake rows
 * of its branches) and folds them again: every log's inserts, then every log's changes, then the
 * feed under a new epoch, offsets in (branch_id, seq) order.
 */
export function rebuildTeamIndex(
  store: LogStore,
  teamId: TeamId,
  outer?: Tx,
): Promise<Result<void, LogError>> {
  return atomically(outer ?? store.driver, async (tx) => {
    const chains = await chainsIn(tx, store, teamId);
    if (!chains.ok) return chains;
    const logs = chains.value.map((c) => ({
      ...c,
      events: knownEvents(c.chain),
    }));
    const broken = checkTeamLogs(logs, teamId);
    if (broken !== undefined)
      return err(
        logError(
          "invalid_transition",
          `branch ${broken.branchId}: ${broken.message}`,
          broken.seq,
        ),
      );
    const epoch = await nextEpoch(tx, teamId);
    if (!epoch.ok) return epoch;
    await wipe(tx, teamId);
    for (const log of logs) await refoldWakes(tx, log.branchId, log.events);
    for (const log of logs) await insertRows(tx, log, log.events, teamId);
    for (const log of logs)
      await changeRows(
        tx,
        log,
        log.events,
        turnOpeners(log.chain.events),
        teamId,
      );
    const feed = logs
      .flatMap((log) => log.events.map((e) => [log.branchId, e.seq] as const))
      .toSorted(([a, x], [b, y]) => byText(a, b) || x - y);
    for (const [i, [branch, seq]] of feed.entries())
      await tx.run(
        "INSERT INTO team_feed (team_id, epoch, feed_offset, branch_id, seq) VALUES (?, ?, ?, ?, ?)",
        [teamId, epoch.value, i + 1, branch, seq],
      );
    return ok(undefined);
  });
}

async function wipe(tx: Tx, teamId: TeamId): Promise<void> {
  for (const table of TEAM_TABLES)
    await tx.run(`DELETE FROM ${table} WHERE team_id = ?`, [teamId]);
}

/** Code-unit order, as SQLite's BINARY collation and Python sort ids. */
const byText = (a: string, b: string): number => (a < b ? -1 : a > b ? 1 : 0);

const Epoch = z.strictObject({ epoch: z.int().nullable() });

/** A rebuilt feed starts a new epoch, so a cursor from an older one restarts. */
async function nextEpoch(
  tx: Tx,
  teamId: TeamId,
): Promise<Result<number, LogError>> {
  const rows = parseRows(
    Epoch,
    await tx.all(
      "SELECT MAX(epoch) AS epoch FROM team_feed WHERE team_id = ?",
      [teamId],
    ),
  );
  return rows.ok ? ok((rows.value[0]?.epoch ?? 0) + 1) : rows;
}
