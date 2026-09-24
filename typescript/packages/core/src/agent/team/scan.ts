import { z } from "zod";
import type { Principal, TeamId } from "../../log";
import { ThreadId } from "../../log";
import type { SqliteDriver } from "../../store/driver";
import { consumable } from "../../team/consume";
import { turnProvenance } from "../../team/provenance";
import {
  type MemberRow,
  memberRows,
  ownRows,
  pendingFor,
  teamRow,
} from "../../team/rows";
import type { VerifiedLog } from "../../verify";
import { ConfigError } from "../errors";

// What the team worker reads to decide its next pass: the teams it drives, which are closed, and
// the principal a member run acts under.

const Row = z.object({ lead_thread_id: ThreadId, team_id: z.string() });

/**
 * The one principal a member run acts under (design §2.6: one turn, one authority): its open
 * turn's, else that of the first mail it would take. Mail of another principal waits for the
 * next run.
 */
export function principalOf(
  db: SqliteDriver,
  log: VerifiedLog,
  row: MemberRow,
): Principal | undefined {
  if (log.fold.turnOpen) return turnProvenance(db, log)?.principal;
  return pendingFor(db, ownRows(db, row.thread_id)).find(consumable)?.provenance
    .principal;
}

/** A definition that can't be set up or pinned here is unavailable: a value, not a throw. */
export async function pinnedOrUnavailable<T>(
  pinned: () => Promise<T>,
): Promise<T | undefined> {
  try {
    return await pinned();
  } catch (error) {
    if (error instanceof ConfigError) return undefined;
    throw error;
  }
}

export function closed(db: SqliteDriver, team: string): boolean {
  return (teamRow(db, team)?.closed_at ?? null) !== null;
}

/** The team and every team led by one of its members, recursively. */
export function teamsUnder(db: SqliteDriver, root: TeamId): readonly string[] {
  const teams: string[] = [root];
  for (let i = 0; i < teams.length; i += 1) {
    const members = memberRows(db, teams[i] ?? "").map((r) => r.thread_id);
    const led = z
      .array(Row)
      .parse(db.all("SELECT lead_thread_id, team_id FROM teams", []))
      .filter(
        (t) => members.includes(t.lead_thread_id) && !teams.includes(t.team_id),
      );
    teams.push(...led.map((t) => t.team_id));
  }
  return teams;
}
