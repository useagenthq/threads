import { z } from "zod";
import { err, ok, type Result } from "../result";
import type { Appended } from "../store/indexing";
import { ALREADY_OPEN, openBranch } from "../store/open";
import { parseRows } from "../store/tables";
import { type LogError, logError } from "../verify/error";
import { changeRows, insertRows } from "./index";
import { teamAppended } from "./wake";

// The team index on the write path (spec/schema/README.md, "Teams"): the index hooks every
// append runs in its transaction, with the same insertRows/changeRows a rebuild folds, so the
// replay rule holds by construction.

/**
 * The team rows the append's events insert, then the rows they change, on a team's own branch
 * only: one that opens with this append, a member's or lead's (its `team_members.branch_id`), or
 * a team log. A fork of a lead or member shares its thread but writes no team rows, so the index
 * stays the fold of the team's own logs (team fork is deferred).
 */
export async function teamRows(a: Appended): Promise<Result<void, LogError>> {
  const opens = a.events.some(
    (e) => e.type === "thread_started" || e.type === "team_opened",
  );
  if (!opens) {
    const teams = await teamsOf(a);
    if (!teams.ok) return teams;
    if (teams.value.length === 0) return ok(undefined);
  }
  const taken = await takenTeam(a);
  if (taken !== undefined)
    return err(
      logError(
        "invalid_transition",
        `team ${taken} already exists; a lead's first append opens it`,
      ),
    );
  const log = { threadId: a.threadId, branchId: a.branchId };
  await insertRows(a.tx, log, a.events);
  await changeRows(a.tx, log, a.events, a.opened);
  return ok(undefined);
}

/**
 * A lead's first append (its thread_started names a team) opens the team log with team_opened,
 * in the same transaction: the teams row comes from it, the lead's row from the thread_started.
 * Nothing but this append opens that branch, so finding it open is refused.
 */
export async function openTeamLog(
  a: Appended,
): Promise<Result<void, LogError>> {
  for (const e of a.events) {
    const team = e.type === "thread_started" ? e.data.team : undefined;
    if (e.type !== "thread_started" || team === undefined) continue;
    const opened = await openBranch(a.tx, a.now, {
      tenantId: a.tenant,
      threadId: team.log_thread_id,
      branchId: team.log_branch_id,
      // Free at once: the next writer (an operator, the team worker) takes epoch 2.
      lease: { holderId: a.holderId, ttlMs: 0 },
      drafts: [
        {
          type: "team_opened",
          type_version: 1,
          critical: true,
          actor: { kind: "host" },
          data: {
            team: team.id,
            lead: {
              tenant: a.tenant,
              team: team.id,
              name: e.data.agent_name,
              generation: 1,
            },
            lead_thread_id: a.threadId,
          },
        },
      ],
    });
    if (!opened.ok) return opened;
    if (opened.value === ALREADY_OPEN)
      return err(
        logError(
          "invalid_transition",
          `team log ${team.log_branch_id} already exists; a lead's first append opens it`,
        ),
      );
  }
  return ok(undefined);
}

/** A team a lead's thread_started names that the store already holds: a second root for it. */
async function takenTeam(a: Appended): Promise<string | undefined> {
  for (const e of a.events) {
    const team = e.type === "thread_started" ? e.data.team : undefined;
    if (team === undefined) continue;
    const found = await a.tx.all("SELECT 1 FROM teams WHERE team_id = ?", [
      team.id,
    ]);
    if (found.length > 0) return team.id;
  }
  return undefined;
}

const TeamRow = z.strictObject({ team_id: z.string() });

/** The teams whose own branch this is: a member's or lead's, or the team log. */
async function teamsOf(
  a: Appended,
): Promise<Result<readonly { team_id: string }[], LogError>> {
  return parseRows(
    TeamRow,
    await a.tx.all(
      `SELECT team_id FROM team_members WHERE thread_id = ? AND branch_id = ?
        UNION SELECT team_id FROM teams WHERE team_log_branch_id = ?`,
      [a.threadId, a.branchId, a.branchId],
    ),
  );
}

/**
 * One team_feed row per appended event, under every team the branch belongs to: a member's or
 * lead's (a nested lead has two), or the team whose log it is. Offsets continue the team's
 * current epoch.
 */
export async function feedRows(a: Appended): Promise<Result<void, LogError>> {
  const teams = await teamsOf(a);
  if (!teams.ok) return teams;
  for (const { team_id } of teams.value) {
    // Only once the rows are durable: a follower woken here reads committed offsets or nothing.
    a.tx.afterCommit(() => teamAppended(team_id));
    for (const e of a.events)
      await a.tx.run(
        `INSERT INTO team_feed (team_id, epoch, feed_offset, branch_id, seq)
          SELECT CAST(? AS TEXT), e, 1 + COALESCE(
            (SELECT MAX(feed_offset) FROM team_feed WHERE team_id = ? AND epoch = e), 0),
            CAST(? AS TEXT), CAST(? AS BIGINT)
          FROM (SELECT COALESCE(MAX(epoch), 1) AS e FROM team_feed WHERE team_id = ?) AS current_epoch`,
        [team_id, team_id, a.branchId, e.seq, team_id],
      );
  }
  return ok(undefined);
}
