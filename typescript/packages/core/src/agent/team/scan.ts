import { z } from "zod";
import {
  type BranchId,
  type Principal,
  type TeamId,
  ThreadId,
} from "../../log";
import type { LogStore } from "../../store";
import { reading, type StoreDriver } from "../../store/driver";
import { getLease } from "../../store/tables";
import { nextDeadline } from "../../team/deadline";
import { turnProvenance } from "../../team/provenance";
import {
  type MemberRow,
  memberRows,
  ownRows,
  pendingFor,
  teamRow,
} from "../../team/rows";
import type { VerifiedLog } from "../../verify";
import { ConfigError, Unbound } from "../errors";

// What the team worker reads to decide its next pass, each in a read-only transaction of its
// own: the teams it drives, which are closed, the principal a member run acts under, a branch's
// lease, and a parked member's next deadline.

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
    return pending[0]?.provenance.principal;
  });
}

/**
 * A definition whose setup failed here (an MCP connect, an adapter's setup) is tried again later:
 * a value, not a throw, and never a failed rebind. A recorded choice naming a tool or model the
 * template no longer has is unbound.
 */
export async function pinnedOrLater<T>(
  pinned: () => Promise<T>,
): Promise<T | "later" | "unbound"> {
  try {
    return await pinned();
  } catch (error) {
    if (error instanceof Unbound) return "unbound";
    if (error instanceof ConfigError) return "later";
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

/** The branch's lease is free: expired or released. */
export async function leaseFree(
  log: LogStore,
  branch: string,
): Promise<boolean> {
  const lease = await reading(log.driver, (tx) => getLease(tx, branch));
  return lease.ok && (lease.value?.expires_at ?? 0) <= log.now();
}

/**
 * An ask or a wait the parked member waits on is due: its writer closes it.
 * ponytail: reads the member's log each pass; keep its next deadline per head if parked members
 * grow many.
 */
export async function deadlineDue(
  log: LogStore,
  branch: BranchId,
): Promise<boolean> {
  const read = await log.read(branch);
  if (!read.ok) return false;
  const chain = read.value;
  const next = await reading(log.driver, (tx) =>
    nextDeadline({ tx, chain, branchId: branch }),
  );
  return next !== undefined && next <= log.now();
}
