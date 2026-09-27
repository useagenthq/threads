import type { Principal } from "../../log";
import { err, ok, type Result } from "../../result";
import { reading } from "../../store/driver";
import { hostTeamIds } from "../../team/host-team";
import { teamRow } from "../../team/rows";
import type { MemberEntry } from "../registry";
import { openStore, type Store, tenantStore } from "../sqlite";
import { teamHandle } from "./handle";
import type { Team } from "./handle-types";

// Host.team({principal}) (spec/api.json Host.team): a handle on the principal's tenant's host
// team. A host team is leadless, so nothing is rebound here — `openTeam`'s config-hash lead rebind
// is never on this path — and the members come from the host's own registry. not_found when the
// tenant has no host team yet (a host with no members option never opens one).

export type HostTeamError = {
  readonly code: "not_found";
  readonly message: string;
};

export async function hostTeam(
  store: Store,
  principal: Principal,
  options: {
    readonly limits: { readonly concurrent: number; readonly mailbox: number };
    /** What each host member is rebound and run with, by agent name (HostRunner.member). */
    readonly agents: ReadonlyMap<string, MemberEntry>;
  },
): Promise<Result<Team, HostTeamError>> {
  const { tenant } = principal;
  const { teamId } = hostTeamIds(tenant);
  const scoped = tenantStore(store, tenant);
  const { log, artifacts } = await openStore(scoped);
  const row = await reading(log.driver, (tx) => teamRow(tx, teamId));
  if (row === undefined || row.kind !== "host")
    return err({
      code: "not_found",
      message: `tenant ${tenant} has no host team; give host() a members option`,
    });
  return ok(
    teamHandle({
      log,
      artifacts,
      ref: { tenant, id: teamId },
      principal,
      store: scoped,
      limits: options.limits,
      agents: options.agents,
    }),
  );
}
