import { z } from "zod";
import type { TeamId } from "../log";
import { knownEvents } from "../reduce";
import { err, ok, type Result } from "../result";
import type { LogStore } from "../store";
import { TEAM_TABLES } from "../store/deletion";
import { READ_ONLY, type Tx } from "../store/driver";
import { atomically, parseRows } from "../store/tables";
import { refoldWakes } from "../store/wakes";
import { type LogError, logError } from "../verify/error";
import {
  byText,
  chainsIn,
  deletedThreads,
  namesDeletedCaller,
  type TeamChain,
} from "./chains";
import { checkTeamLogs } from "./cross";
import { changeRows, insertRows, turnOpeners } from "./index";

// The replay rule's other half: wipe a team's index rows and fold them again from its logs
// alone, with the same insertRows/changeRows a writer uses, so a rebuild equals what the appends
// wrote (mail's claim columns aside, and team_feed, which starts a new epoch).

export type { TeamChain };

/** The team's logs (chains.ts) read in one read-only transaction. */
export function teamChains(
  store: LogStore,
  teamId: TeamId,
): Promise<Result<readonly TeamChain[], LogError>> {
  return store.driver.transaction(
    (tx) => chainsIn(tx, store, teamId),
    READ_ONLY,
  );
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
    const deleted = await deletedThreads(tx, store.tenant);
    if (!deleted.ok) return deleted;
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
    // A mail or ask naming a tombstoned caller is not rebuilt; a change to a row no log
    // inserted then updates nothing, as SQL's UPDATE does.
    for (const log of logs)
      await insertRows(
        tx,
        log,
        log.events.filter((e) => !namesDeletedCaller(e, deleted.value)),
        teamId,
      );
    for (const log of logs)
      await changeRows(
        tx,
        log,
        log.events,
        turnOpeners(log.chain.events),
        teamId,
      );
    await refillFeed(tx, teamId, epoch.value, logs);
    return ok(undefined);
  });
}

/**
 * One feed row per event of every branch the team's feed holds: a member's or the lead's, or the
 * team log. A caller is in no team, so its events are in no feed.
 */
async function refillFeed(
  tx: Tx,
  teamId: TeamId,
  epoch: number,
  logs: readonly {
    readonly branchId: string;
    readonly events: readonly { readonly seq: number }[];
  }[],
): Promise<void> {
  const rows = parseRows(
    z.strictObject({ branch_id: z.string() }),
    await tx.all(
      `SELECT branch_id FROM team_members WHERE team_id = ? AND branch_id IS NOT NULL
        UNION SELECT team_log_branch_id AS branch_id FROM teams WHERE team_id = ?`,
      [teamId, teamId],
    ),
  );
  const inFeed = new Set<string>(
    rows.ok ? rows.value.map((r) => r.branch_id) : [],
  );
  const feed = logs
    .filter((log) => inFeed.has(log.branchId))
    .flatMap((log) => log.events.map((e) => [log.branchId, e.seq] as const))
    .toSorted(([a, x], [b, y]) => byText(a, b) || x - y);
  for (const [i, [branch, seq]] of feed.entries())
    await tx.run(
      "INSERT INTO team_feed (team_id, epoch, feed_offset, branch_id, seq) VALUES (?, ?, ?, ?, ?)",
      [teamId, epoch, i + 1, branch, seq],
    );
}

async function wipe(tx: Tx, teamId: TeamId): Promise<void> {
  for (const table of TEAM_TABLES)
    await tx.run(`DELETE FROM ${table} WHERE team_id = ?`, [teamId]);
}

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
