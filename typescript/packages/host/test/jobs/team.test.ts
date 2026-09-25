import { afterEach, describe, expect, test } from "bun:test";
import { join } from "node:path";
import { z } from "zod";
import { sqlAll } from "../sql";
import {
  expireLeases,
  finish,
  go,
  kill,
  reap,
  scratch,
  spawn,
  waitAt,
} from "./drill";
import { drillDriver } from "./stores";
import { RACED, RACER_THREAD, TEAM, TEAM_LOG } from "./team-worker";

// Team store drills on real processes: a lead killed with SIGKILL inside its first append leaves
// nothing of it (all-or-nothing), and two processes racing branch.open on one branch leave one
// opener, the other told already_open, and no orphan.

afterEach(reap);

const WORKER = join(import.meta.dir, "team-worker.ts");
const TEAM_RUN = join(import.meta.dir, "team-run.ts");
const Rows = z.array(z.record(z.string(), z.unknown()));

async function query(
  where: string,
  sql: string,
  params: readonly string[] = [],
): Promise<unknown> {
  const db = (await drillDriver(where)).db;
  try {
    return Rows.parse(await sqlAll(db, sql, params));
  } finally {
    await db.close();
  }
}

async function output(where: string, role: string): Promise<string> {
  const w = spawn(role, where, {}, WORKER);
  const line = await w.lines.next();
  expect(await finish(w)).toBe(0);
  return String(line.value);
}

describe("team store drills", () => {
  test("a lead killed inside its first append leaves nothing; the retry opens the team whole", async () => {
    const dir = scratch();
    const first = spawn(
      "lead",
      dir,
      { DRILL_STOP_AT: "lead_first_append" },
      WORKER,
    );
    await waitAt(first, "lead_first_append");
    await kill(first);
    for (const table of [
      "branches",
      "events",
      "leases",
      "teams",
      "team_members",
      "team_feed",
    ])
      expect(await query(dir, `SELECT * FROM ${table}`)).toEqual([]);

    expect(await output(dir, "lead")).toBe("opened");
    expect(
      await query(dir, "SELECT team_id, team_log_branch_id FROM teams"),
    ).toEqual([{ team_id: TEAM, team_log_branch_id: TEAM_LOG }]);
    expect(await query(dir, "SELECT name, role FROM team_members")).toEqual([
      { name: "lead", role: "lead" },
    ]);
    expect(
      await query(dir, "SELECT type FROM events WHERE branch_id = ?", [
        TEAM_LOG,
      ]),
    ).toEqual([{ type: "team_opened" }]);
  });

  test("two processes racing branch.open: one opens, the other is already_open, no orphan", async () => {
    const dir = scratch();
    const a = spawn("open-a", dir, {}, WORKER);
    const b = spawn("open-b", dir, {}, WORKER);
    go(dir);
    const said = await Promise.all(
      [a, b].map(async (w) => String((await w.lines.next()).value)),
    );
    expect(await finish(a)).toBe(0);
    expect(await finish(b)).toBe(0);
    expect(said.toSorted()).toEqual(["already_open", "opened"]);
    const winner = said[0] === "opened" ? "open-a" : "open-b";
    expect(await query(dir, "SELECT thread_id FROM threads")).toEqual([
      { thread_id: RACER_THREAD[winner] },
    ]);
    expect(
      await query(dir, "SELECT branch_id, holder_id, epoch FROM leases"),
    ).toEqual([{ branch_id: RACED, holder_id: winner, epoch: 1 }]);
    expect(
      await query(dir, "SELECT seq, type FROM events ORDER BY seq"),
    ).toEqual([
      { seq: 1, type: "thread_started" },
      { seq: 2, type: "user_input" },
    ]);
  });

  test("a lead killed inside its member's settlement: the restart settles the member once and answers", async () => {
    const dir = scratch();
    const first = spawn(
      "run",
      dir,
      { DRILL_STOP_AT: "member_settle" },
      TEAM_RUN,
    );
    await waitAt(first, "member_settle");
    await kill(first);
    // Nothing of the settling append was stored: the member's turn is still open.
    expect(
      await query(
        dir,
        "SELECT name, state FROM team_members WHERE role = 'member'",
      ),
    ).toEqual([{ name: "researcher-1", state: "running" }]);
    await expireLeases(dir);
    const again = spawn("resume", dir, {}, TEAM_RUN);
    const said = await again.lines.next();
    expect(await finish(again)).toBe(0);
    expect(String(said.value)).toBe("completed");
    expect(
      await query(
        dir,
        "SELECT COUNT(*) AS n FROM events WHERE type = 'member_started'",
      ),
    ).toEqual([{ n: 1 }]);
    expect(
      await query(
        dir,
        "SELECT COUNT(*) AS n FROM events WHERE type = 'member_idle' OR type = 'member_ended'",
      ),
    ).toEqual([{ n: 3 }]);
    expect(
      await query(
        dir,
        "SELECT COUNT(*) AS n FROM mail WHERE kind IN ('member_settled', 'member_ended') AND state = 'consumed'",
      ),
    ).toEqual([{ n: 1 }]);
  });
});
