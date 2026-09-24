import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { agent, scriptedModel } from "../../src";
import { hostRunner } from "../../src/agent/registry";
import { storeOf } from "../../src/agent/sqlite";
import { BranchId, ThreadId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import {
  LogStore,
  memoryArtifacts,
  type SqliteDriver,
  type SqlValue,
} from "../../src/store";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { memberRows, teamRow } from "../../src/team/rows";
import { unwrap } from "../store/helpers";
import { assertTeamReplays } from "./kit";
import { say, start } from "./run-kit";

// Crash drills at each commit point of a team run (design §7, Phase 1 proofs): the process dies
// inside the transaction that commits the lead's start, a member's materialize, the member's
// settlement, or the lead's receipt of it. Nothing of that transaction is stored; the restart
// (the host's recovery of the open run, no new input) finishes the run with exactly one start, one
// member branch, one task input and one settlement received.

class Crash extends Error {}

type Point = {
  readonly name: string;
  readonly at: (sql: string, params: readonly SqlValue[]) => boolean;
};

const utf8 = new TextDecoder();
const mentions = (params: readonly SqlValue[], text: string): boolean =>
  params.some(
    (p) =>
      (typeof p === "string" && p.includes(text)) ||
      (p instanceof Uint8Array && utf8.decode(p).includes(text)),
  );

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

/** The same database, through a driver that dies once at `point`. */
function crashing(base: SqliteDriver, point: Point): SqliteDriver {
  let crashed = false;
  return {
    ...base,
    run: (sql, params) => {
      if (!crashed && point.at(sql, params)) {
        crashed = true;
        throw new Crash(`killed at ${point.name}`);
      }
      base.run(sql, params);
    },
  };
}

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

const Row = z.object({ thread_id: ThreadId });

describe("team crash drills", () => {
  for (const point of POINTS)
    test(`a crash inside ${point.name} stores none of it; the restart finishes once`, async () => {
      const db = openBunSqlite(":memory:");
      const artifacts = memoryArtifacts();
      const open = (driver: SqliteDriver) =>
        storeOf({
          log: unwrap(LogStore.open(driver, Date.now, artifacts)),
          artifacts,
        });
      const first = team("fresh", ["Done."]);
      await expect(
        first.run("Work.", { store: open(crashing(db, point)) }),
      ).rejects.toThrow(Crash);

      const store = open(db);
      const log = unwrap(LogStore.open(db, Date.now, artifacts));
      const [row] = z
        .array(Row.extend({ team_id: z.string() }))
        .parse(
          db.all("SELECT lead_thread_id AS thread_id, team_id FROM teams", []),
        );
      if (row === undefined)
        throw new Error("the lead's first append opened its team");
      const leadBranch = unwrap(log.mainBranch(row.thread_id));
      const restarted = team("restart", ["Done.", "Done."]);
      const runner = hostRunner(restarted);
      if (runner === undefined)
        throw new Error("agent() registers a host runner");
      const result = await runner.execute(
        {
          store,
          principal: { issuer: "api", tenant: "local", subject: "operator" },
          thread: { id: row.thread_id, branch: leadBranch, store },
        },
        [],
      );
      expect(result.status).toBe("completed");

      const lead = knownEvents(unwrap(log.read(leadBranch)));
      expect(lead.filter((e) => e.type === "member_started")).toHaveLength(1);
      const members = memberRows(db, row.team_id).filter(
        (r) => r.role === "member",
      );
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
      expect(teamRow(db, row.team_id)?.closed_at).toBeNull();
      assertTeamReplays(log, z.string().brand<"TeamId">().parse(row.team_id));
    });
});
