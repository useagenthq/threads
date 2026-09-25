import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { agent, type Model, scriptedModel, sqlite } from "../../src";
import { BranchId, TeamId } from "../../src/log";
import { markTestKit } from "../../src/model/guard";
import { knownEvents } from "../../src/reduce";
import { memberRows } from "../../src/team/rows";
import { unwrap } from "../store/helpers";
import { assertTeamReplays, query, reading } from "./kit";
import { logOf, receipts, say, start } from "./run-kit";

// A nested lead (spec/schema/README.md, "Teams"): a member whose own definition has a team starts
// members of its own team. The lead's worker runs the nested team too; the nested member's
// settlement wakes the nested lead, and both teams replay.

/** A scripted model that runs `on(n)` before its nth request (from 1). */
function watched(
  responses: readonly unknown[],
  on: (n: number) => Promise<void> | void,
): Model {
  const model = scriptedModel({ responses: [...responses] });
  let n = 0;
  const made: Model = {
    ...model,
    send: async function* (...args: Parameters<Model["send"]>) {
      n += 1;
      await on(n);
      yield* model.send(...args);
    },
  };
  markTestKit(made);
  return made;
}

const Team = z.array(z.object({ team_id: TeamId, lead_thread_id: z.string() }));

describe("a nested lead", () => {
  test("starts its own member, which wakes it when it settles; both teams replay", async () => {
    const store = sqlite(":memory:");
    const woke = Promise.withResolvers<void>();
    const scanner = agent({
      name: "scanner",
      model: scriptedModel({ responses: [say("Scanned.")] }),
    });
    const researcher = agent({
      name: "researcher",
      model: watched(
        [
          start("r1", "scanner", "Scan the notes."),
          say("Waiting for the scanner."),
          say("The scan is done."),
        ],
        (n) => {
          if (n === 3) woke.resolve();
        },
      ),
      team: [scanner],
    });
    const lead = agent({
      name: "lead",
      model: watched(
        [
          start("c1", "researcher", "Research."),
          say("Started."),
          say("Final."),
        ],
        // The lead's final answer waits until the scanner's settlement has woken the researcher.
        async (n) => {
          if (n === 3) await woke.promise;
        },
      ),
      team: [researcher],
    });
    const r = await lead.run("Work.", { store });
    expect(r.status === "completed" && r.output).toBe("Final.");

    const log = await logOf(store);
    const teams = Team.parse(
      await query(
        log.driver,
        "SELECT team_id, lead_thread_id FROM teams ORDER BY team_id",
        [],
      ),
    );
    expect(teams).toHaveLength(2);
    const outer = r.team.ref.id;
    const row = (await reading(log.driver, (tx) => memberRows(tx, outer))).find(
      (m) => m.name === "researcher-1",
    );
    const inner = teams.find(
      (t) => t.lead_thread_id === row?.thread_id,
    )?.team_id;
    if (row?.branch_id === null || row === undefined || inner === undefined)
      throw new Error("the researcher leads its own team");
    const nested = knownEvents(
      unwrap(await log.read(BranchId.parse(row.branch_id))),
    );
    expect(receipts(nested, "member_settled")).toHaveLength(1);
    expect(
      (await reading(log.driver, (tx) => memberRows(tx, inner))).map(
        (m): string => m.name,
      ),
    ).toEqual(["researcher", "scanner-1"]);
    await assertTeamReplays(log, outer);
    await assertTeamReplays(log, inner);
  });
});
