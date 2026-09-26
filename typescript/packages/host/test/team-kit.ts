import { openStore, type Store, tenantStore } from "@threads/core/host";
import { TeamId } from "../../core/src/log";
import { assertTeamReplays } from "../../core/test/team/kit";
import { knownEventsOf } from "./kit";

// What the host's team tests share: the team a lead thread opened, and lane 21's replay rule.

/** The team of the lead's thread, from its pinned thread_started. */
export async function teamOf(
  store: Store,
  tenant: string,
  branch: string,
): Promise<TeamId> {
  const started = (await knownEventsOf(store, tenant, branch)).find(
    (e) => e.type === "thread_started",
  );
  if (started?.type !== "thread_started" || started.data.team === undefined)
    throw new Error("the lead's thread names no team");
  return TeamId.parse(started.data.team.id);
}

/** Lane 21's rule: every log verifies, and a wipe and rebuild of the index is byte-equal. */
export async function assertReplays(
  store: Store,
  tenant: string,
  team: TeamId,
): Promise<void> {
  const { log } = await openStore(tenantStore(store, tenant));
  await assertTeamReplays(log, team);
}
