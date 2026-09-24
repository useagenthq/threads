import { describe, expect, test } from "bun:test";
import { agent, scriptedModel } from "../../src";
import { BranchId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { memberRows, teamRow } from "../../src/team/rows";
import { unwrap } from "../store/helpers";
import { Crash, crashing, drill, mentions, type Point } from "./crash-kit";
import { assertTeamReplays } from "./kit";
import { say, start } from "./run-kit";

// Crash drills at each commit point of a team run (design §7, Phase 1 proofs): the process dies
// inside the transaction that commits the lead's start, a member's materialize, the member's
// settlement, or the lead's receipt of it. Nothing of that transaction is stored; the restart
// (the host's recovery of the open run, no new input) finishes the run with exactly one start, one
// member branch, one task input and one settlement received.

const POINTS: readonly Point[] = [
  {
    name: "the lead's start",
    at: (sql, params) =>
      sql.includes("INSERT INTO team_members") &&
      mentions(params, "researcher-1"),
  },
  {
    name: "the member's materialize",
    at: (sql) => sql.includes("UPDATE team_members SET branch_id"),
  },
  {
    name: "the member's settlement",
    at: (sql, params) =>
      sql.includes("UPDATE team_members SET result") &&
      mentions(params, "researcher-1"),
  },
  {
    name: "the lead's receipt of the settlement",
    at: (sql, params) =>
      sql.includes("UPDATE mail SET state = 'consumed'") &&
      !params.some((p) => typeof p === "string" && p.endsWith(":c1")),
  },
];

function team(lead: string, researcher: readonly string[]) {
  return agent({
    name: "lead",
    model: scriptedModel({
      responses: [
        ...(lead === "fresh" ? [start("c1", "researcher", "Go.")] : []),
        say("Started."),
        say("Final."),
        say("Final."),
      ],
    }),
    team: [
      agent({
        name: "researcher",
        model: scriptedModel({ responses: researcher.map((t) => say(t)) }),
      }),
    ],
  });
}

describe("team crash drills", () => {
  for (const point of POINTS)
    test(`a crash inside ${point.name} stores none of it; the restart finishes once`, async () => {
      const d = drill();
      const first = team("fresh", ["Done."]);
      await expect(
        first.run("Work.", { store: d.open(crashing(d.db, point)) }),
      ).rejects.toThrow(Crash);
      const {
        result,
        branch: leadBranch,
        team: teamId,
      } = await d.restart(team("restart", ["Done.", "Done."]));
      expect(result.status).toBe("completed");
      const { db, log } = d;
      const lead = knownEvents(unwrap(log.read(leadBranch)));
      expect(lead.filter((e) => e.type === "member_started")).toHaveLength(1);
      const members = memberRows(db, teamId).filter((r) => r.role === "member");
      expect(members.map((r): string => r.name)).toEqual(["researcher-1"]);
      const branch = members[0]?.branch_id;
      if (branch === null || branch === undefined)
        throw new Error("materialized");
      const member = knownEvents(unwrap(log.read(BranchId.parse(branch))));
      expect(member.filter((e) => e.type === "user_input")).toHaveLength(1);
      const notices = lead.filter(
        (e) =>
          e.type === "message_received" &&
          (e.data.envelope.kind === "member_settled" ||
            e.data.envelope.kind === "member_ended"),
      );
      expect(notices).toHaveLength(1);
      expect(teamRow(db, teamId)?.closed_at).toBeNull();
      assertTeamReplays(log, teamId);
    });
});
