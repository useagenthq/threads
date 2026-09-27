import { z } from "zod";
import { BranchId, TeamId, ThreadId } from "../../log";
import type { LogStore } from "../../store";
import type { ArtifactStore } from "../../store/artifacts";
import { reading, type StoreDriver } from "../../store/driver";
import type { DeferTools } from "../defer";
import type { MemberEntry } from "../registry";
import type { Store } from "../sqlite";
import { rebound } from "./open";
import { leadOf } from "./operator-request";
import { agentsOf, memberEntries } from "./runtime";
import { TeamWorker } from "./worker";

// What a host needs to run lane 21's team worker for a team no run of its own opened (design
// §7 Phase 2, "The host is a team worker"): the teams to drive, and a worker for one of them.
// The host calls the store operations lane 21 built; this adds none.

/** An open team a host drives, with the lead it wakes (design §2.7.2). */
export type HostedTeam = {
  readonly team_id: TeamId;
  readonly tenant_id: string;
  readonly lead_thread_id: ThreadId;
  /** Null until the lead's first append; its worker has nothing to drive until then. */
  readonly lead_branch_id: BranchId | null;
};

const Row = z.strictObject({
  team_id: TeamId,
  tenant_id: z.string(),
  lead_thread_id: ThreadId,
  lead_branch_id: BranchId.nullable(),
});

/**
 * Every open team a host drives: the roots, whose lead is in no other team. A nested lead is a
 * member of its parent's team, and that team's worker drives it (teamsUnder). A host team is
 * leadless and has its own listing (hostTeams).
 */
export async function hostedTeams(
  db: StoreDriver,
): Promise<readonly HostedTeam[]> {
  const rows = await reading(db, (tx) =>
    tx.all(
      `SELECT t.team_id, t.tenant_id, m.thread_id AS lead_thread_id, m.branch_id AS lead_branch_id
         FROM teams t JOIN team_members m ON m.team_id = t.team_id AND m.role = 'lead'
        WHERE t.closed_at IS NULL AND t.kind = 'lead'
          AND NOT EXISTS (SELECT 1 FROM team_members n
                           WHERE n.thread_id = m.thread_id AND n.role = 'member')
        ORDER BY t.team_id`,
    ),
  );
  return z.array(Row).parse(rows);
}

/** A tenant's host team (Teams Phase 2): leadless, so nothing about a lead is read for it. */
export type HostTeam = {
  readonly team_id: TeamId;
  readonly tenant_id: string;
};

const HostRow = z.strictObject({ team_id: TeamId, tenant_id: z.string() });

/**
 * Every tenant's host team. It has no lead row, so its worker's agents come from the host's own
 * registry (the policy-aware definitions HostRunner.withPolicy builds), never from a lead rebind.
 */
export async function hostTeams(db: StoreDriver): Promise<readonly HostTeam[]> {
  const rows = await reading(db, (tx) =>
    tx.all(
      `SELECT team_id, tenant_id FROM teams
        WHERE kind = 'host' AND closed_at IS NULL ORDER BY team_id`,
    ),
  );
  return z.array(HostRow).parse(rows);
}

const CallerRow = z.strictObject({
  thread_id: ThreadId,
  to_branch_id: BranchId,
});

/**
 * The caller branches of a host team with pending mail: their own logs are the only record of what
 * they are waiting on, so the host wakes each to consume its reply or bounce (Teams Phase 2).
 */
export async function pendingCallers(
  db: StoreDriver,
  team: TeamId,
): Promise<
  readonly { readonly thread_id: ThreadId; readonly to_branch_id: BranchId }[]
> {
  const rows = await reading(db, (tx) =>
    tx.all(
      `SELECT DISTINCT b.thread_id, m.to_branch_id FROM mail m
         JOIN branches b ON b.branch_id = m.to_branch_id
        WHERE m.team_id = ? AND m.to_kind = 'caller' AND m.state = 'pending'
        ORDER BY m.to_branch_id`,
      [team],
    ),
  );
  return z.array(CallerRow).parse(rows);
}

export type TeamWorkerEnv = {
  readonly store: Store;
  readonly log: LogStore;
  readonly artifacts: ArtifactStore;
  readonly team: TeamId;
  /** Aborted when the host stops: every member run in flight gives up on it. */
  readonly signal?: AbortSignal;
};

/** What a team's worker needs of its lead: pinning it is the costly part, so it is kept. */
export type TeamLead = {
  readonly agents: ReadonlyMap<string, MemberEntry>;
  readonly deferTools: DeferTools | undefined;
};

/**
 * The lead of the team, as this process defines it: rebound by name and config_hash, as
 * materialize rebinds a member (openTeam's rule), with the defer_tools its members inherit.
 * Undefined when this process doesn't define that lead, so another host drives the team.
 */
export async function teamLeadOf(
  log: LogStore,
  team: TeamId,
): Promise<TeamLead | undefined> {
  const lead = await rebound(log, { tenant: log.tenant, id: team });
  if (!lead.ok) return undefined;
  const { deferTools } = await leadOf(log, team);
  return { agents: memberEntries(agentsOf(lead.value.team ?? [])), deferTools };
}

/** The team's worker, as a process that didn't start the team runs it. */
export function teamWorkerFor(env: TeamWorkerEnv, lead: TeamLead): TeamWorker {
  return new TeamWorker({
    store: env.store,
    log: env.log,
    artifacts: env.artifacts,
    team: env.team,
    agents: lead.agents,
    ...(env.signal === undefined ? {} : { signal: env.signal }),
    ...(lead.deferTools === undefined ? {} : { deferTools: lead.deferTools }),
  });
}
