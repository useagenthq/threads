import { z } from "zod";
import { type Principal, type TeamId, ThreadId } from "../../log";
import { reading, type StoreDriver } from "../../store/driver";
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

// What the team worker reads to decide its next pass, each in a read-only transaction of its
// own: the teams it drives, which are closed, and the principal a member run acts under.

const Row = z.object({ lead_thread_id: ThreadId, team_id: z.string() });

/**
 * The one principal a member run acts under (design §2.6: one turn, one authority): its open
 * turn's, else that of the first mail it would take. Mail of another principal waits for the
 * next run.
 */
export function principalOf(
  db: StoreDriver,
  log: VerifiedLog,
  row: MemberRow,
): Promise<Principal | undefined> {
  return reading(db, async (tx) => {
    if (log.fold.turnOpen) return (await turnProvenance(tx, log))?.principal;
    const pending = await pendingFor(tx, await ownRows(tx, row.thread_id));
    return pending.find(consumable)?.provenance.principal;
  });
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

export async function closed(db: StoreDriver, team: string): Promise<boolean> {
  const row = await reading(db, (tx) => teamRow(tx, team));
  return (row?.closed_at ?? null) !== null;
}

/** The team and every team led by one of its members, recursively. */
export async function teamsUnder(
  db: StoreDriver,
  root: TeamId,
): Promise<readonly string[]> {
  const teams: string[] = [root];
  for (let i = 0; i < teams.length; i += 1) {
    const [rows, all] = await reading(db, async (tx) => [
      await memberRows(tx, teams[i] ?? ""),
      await tx.all("SELECT lead_thread_id, team_id FROM teams"),
    ]);
    const members = rows.map((r) => r.thread_id);
    const led = z
      .array(Row)
      .parse(all)
      .filter(
        (t) => members.includes(t.lead_thread_id) && !teams.includes(t.team_id),
      );
    teams.push(...led.map((t) => t.team_id));
  }
  return teams;
}
