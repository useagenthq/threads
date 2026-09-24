import type { Principal } from "../../log";
import { err, ok, type Result } from "../../result";
import { memberRows, teamRow } from "../../team/rows";
import { leadNamed, memberEntry } from "../registry";
import { openStore, type Store, tenantStore } from "../sqlite";
import { teamHandle } from "./handle";
import type { Team, TeamRef } from "./handle-types";

// openTeam (spec/api.json openTeam): a handle on a team another process ran, or to act as another
// principal. RunResult.team is the common path.

export type OpenTeamError = {
  readonly code: "not_found" | "forbidden";
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
  const { log, artifacts } = await openStore(tenantStore(store, ref.tenant));
  const team = teamRow(log.driver, ref.id);
  if (team === undefined || team.tenant_id !== ref.tenant)
    return err({
      code: "not_found",
      message: `no team ${ref.id} in ${ref.tenant}`,
    });
  // The team's agents are its lead's, as this process defines the lead of that name.
  const lead = memberRows(log.driver, ref.id).find((r) => r.role === "lead");
  const defined = lead === undefined ? undefined : leadNamed(lead.agent);
  return ok(
    teamHandle({
      log,
      artifacts,
      ref,
      principal,
      lead: defined === undefined ? undefined : memberEntry(defined),
    }),
  );
}
