import { z } from "zod";
import {
  BranchId,
  MemberName,
  PosInt,
  StoredMemberResult,
  type TeamId,
} from "../../log";
import { knownEvents } from "../../reduce";
import type { LogStore } from "../../store";
import type { ArtifactStore } from "../../store/artifacts";
import { reading } from "../../store/driver";
import { teamRow } from "../../team/rows";
import type { MemberState, TeamMember } from "./handle-types";
import { hydrated } from "./hydrate";

// team.members() (spec/api.json Team.members): a pure read of the team's rows, the lead's
// included, in (name, generation) order. A settled member's result is hydrated; a label is read
// from the member_started that started it (the lead's log, or the team log's for an operator's).

const utf8 = new TextDecoder();

const Row = z.strictObject({
  name: MemberName,
  generation: PosInt,
  agent: z.string(),
  state: z.enum(["starting", "running", "idle", "parked", "ended"]),
  role: z.enum(["lead", "member"]),
  branch_id: BranchId.nullable(),
  result: z.instanceof(Uint8Array).nullable(),
});

/** Every member of the team. Throws StoreCorruptError when a result's artifact is broken. */
export async function roster(
  log: LogStore,
  artifacts: ArtifactStore,
  team: TeamId,
): Promise<readonly TeamMember[]> {
  const teams = await reading(log.driver, (tx) => teamRow(tx, team));
  if (teams === undefined) return [];
  const rows = z.array(Row).parse(
    await reading(log.driver, (tx) =>
      tx.all(
        `SELECT name, generation, agent, state, role, branch_id, result FROM team_members
          WHERE team_id = ? ORDER BY name, generation`,
        [team],
      ),
    ),
  );
  const lead = rows.find((r) => r.role === "lead")?.branch_id ?? null;
  const labels = await labelsIn(log, [lead, teams.team_log_branch_id]);
  const members: TeamMember[] = [];
  for (const r of rows) {
    const ref = {
      tenant: teams.tenant_id,
      team,
      name: r.name,
      generation: r.generation,
    };
    const label = labels.get(`${r.name}/${r.generation}`);
    const state: MemberState = r.state;
    members.push({
      name: r.name,
      ref,
      agent: r.agent,
      state,
      ...(r.result === null
        ? {}
        : {
            result: await hydrated(
              StoredMemberResult.parse(JSON.parse(utf8.decode(r.result))),
              artifacts,
            ),
          }),
      ...(label === undefined ? {} : { label }),
    });
  }
  return members;
}

/** Each started member's label, by `<name>/<generation>`, from the logs that start members. */
async function labelsIn(
  log: LogStore,
  branches: readonly (string | null)[],
): Promise<ReadonlyMap<string, string>> {
  const out = new Map<string, string>();
  for (const branch of branches) {
    if (branch === null) continue;
    const read = await log.read(BranchId.parse(branch));
    // The index names this log: an unreadable one is a broken store, not an answer.
    if (!read.ok) throw new Error(`team log ${branch}: ${read.error.message}`);
    for (const e of knownEvents(read.value))
      if (e.type === "member_started" && e.data.label !== undefined)
        out.set(
          `${e.data.member.name}/${e.data.member.generation}`,
          e.data.label,
        );
  }
  return out;
}
