import { BranchId, type Principal } from "../../log";
import { knownEvents } from "../../reduce";
import { err, ok, type Result } from "../../result";
import type { LogStore } from "../../store";
import { reading } from "../../store/driver";
import { memberRows, teamRow } from "../../team/rows";
import { ConfigError } from "../errors";
import { leadsNamed, type MemberEntry, memberEntry } from "../registry";
import { asks } from "../run";
import { openStore, type Store, tenantStore } from "../sqlite";
import { teamHandle } from "./handle";
import type { Team, TeamRef } from "./handle-types";

// openTeam (spec/api.json openTeam): a handle on a team another process ran, or to act as another
// principal. RunResult.team is the common path.

export type OpenTeamError = {
  readonly code: "not_found" | "forbidden" | "unavailable";
  readonly message: string;
};

export async function openTeam(
  store: Store,
  ref: TeamRef,
  options: { readonly principal: Principal },
): Promise<Result<Team, OpenTeamError>> {
  const { principal } = options;
  if (principal.tenant !== ref.tenant)
    return err({
      code: "forbidden",
      message: `the principal is of tenant ${principal.tenant}, not the team's`,
    });
  const scoped = tenantStore(store, ref.tenant);
  const { log, artifacts } = await openStore(scoped);
  const team = await reading(log.driver, (tx) => teamRow(tx, ref.id));
  if (team === undefined || team.tenant_id !== ref.tenant)
    return err({
      code: "not_found",
      message: `no team ${ref.id} in ${ref.tenant}`,
    });
  const lead = await rebound(log, ref);
  if (!lead.ok) return lead;
  return ok(
    teamHandle({
      log,
      artifacts,
      ref,
      principal,
      lead: lead.value,
      store: scoped,
    }),
  );
}

/**
 * The lead that ran, as this process defines it: among the leads of its name, the one whose pin
 * has the lead thread's config_hash, as materialize rebinds a member. Anything else would start
 * agents the team never listed, so no match is `unavailable` and nothing is recorded.
 */
export async function rebound(
  log: LogStore,
  ref: TeamRef,
): Promise<Result<MemberEntry, OpenTeamError>> {
  const row = (await reading(log.driver, (tx) => memberRows(tx, ref.id))).find(
    (r) => r.role === "lead",
  );
  const read =
    row?.branch_id === undefined || row.branch_id === null
      ? undefined
      : await log.read(BranchId.parse(row.branch_id));
  if (row === undefined || read === undefined || !read.ok)
    throw new Error(`team ${ref.id} has no readable lead log`);
  const events = knownEvents(read.value);
  const started = events.find((e) => e.type === "thread_started");
  if (started?.type !== "thread_started")
    throw new Error("a lead log starts with thread_started");
  const as = {
    member: started.data.parent?.relation === "team_member",
    answerer: asks(events),
    deferTools: started.data.policy?.context?.defer_tools,
  };
  for (const candidate of leadsNamed(row.agent)) {
    const entry = memberEntry(candidate);
    if (entry !== undefined && (await hashOf(entry, as)) === row.config_hash)
      return ok(entry);
  }
  return err({
    code: "unavailable",
    message: `this process does not define lead ${row.agent} at config ${row.config_hash}; define that agent here to act on its team`,
  });
}

/** A candidate that can't be set up here is no match. */
async function hashOf(
  entry: MemberEntry,
  as: Parameters<MemberEntry["configHash"]>[0],
): Promise<string | undefined> {
  try {
    return await entry.configHash(as);
  } catch (error) {
    if (error instanceof ConfigError) return undefined;
    throw error;
  }
}
