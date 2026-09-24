import { z } from "zod";
import { ThreadId } from "../log";
import { err, ok, type Result } from "../result";
import { uuidv7 } from "../store/encode";
import type { Appended } from "../store/indexing";
import { ALREADY_OPEN, openBranch } from "../store/open";
import { parseRows } from "../store/tables";
import { type LogError, logError } from "../verify/error";
import { changeRows, insertRows } from "./index";

// The team index on the write path (spec/schema/README.md, "Teams"): the index hooks every
// append runs in its transaction, with the same insertRows/changeRows a rebuild folds, so the
// replay rule holds by construction.

/** The rows the append's events insert, then the rows they change (pending_wakes included). */
export function indexRows(a: Appended): Result<void, LogError> {
  const log = { threadId: a.threadId, branchId: a.branchId };
  insertRows(a.db, log, a.events);
  changeRows(a.db, log, a.events, a.opened);
  return ok(undefined);
}

/**
 * A lead's first append (its thread_started names a team) opens the team log with team_opened,
 * in the same transaction: the teams row comes from it, the lead's row from the thread_started.
 * Nothing but this append opens that branch, so finding it open is refused.
 */
export function openTeamLog(a: Appended): Result<void, LogError> {
  for (const e of a.events) {
    const team = e.type === "thread_started" ? e.data.team : undefined;
    if (e.type !== "thread_started" || team === undefined) continue;
    const opened = openBranch(a.db, a.now, {
      tenantId: a.tenant,
      threadId: ThreadId.parse(uuidv7(a.now)),
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

const TeamRow = z.strictObject({ team_id: z.string() });

/**
 * One team_feed row per appended event, under every team the branch belongs to: a member's or
 * lead's (a nested lead has two), or the team whose log it is. Offsets continue the team's
 * current epoch.
 */
export function feedRows(a: Appended): Result<void, LogError> {
  const teams = parseRows(
    TeamRow,
    a.db.all(
      `SELECT team_id FROM team_members WHERE thread_id = ? AND branch_id = ?
        UNION SELECT team_id FROM teams WHERE team_log_branch_id = ?`,
      [a.threadId, a.branchId, a.branchId],
    ),
  );
  if (!teams.ok) return teams;
  for (const { team_id } of teams.value)
    for (const e of a.events)
      a.db.run(
        `INSERT INTO team_feed (team_id, epoch, feed_offset, branch_id, seq)
          SELECT ?, e, 1 + COALESCE(
            (SELECT MAX(feed_offset) FROM team_feed WHERE team_id = ? AND epoch = e), 0), ?, ?
          FROM (SELECT COALESCE(MAX(epoch), 1) AS e FROM team_feed WHERE team_id = ?)`,
        [team_id, team_id, a.branchId, e.seq, team_id],
      );
  return ok(undefined);
}
