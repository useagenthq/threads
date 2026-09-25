import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { agent, type Model, scriptedModel } from "../../src";
import { storeOf } from "../../src/agent/sqlite";
import { BranchId } from "../../src/log";
import { markTestKit } from "../../src/model/guard";
import { LogStore, memoryArtifacts } from "../../src/store";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { TEAM_CONSTANTS } from "../../src/team/constants";
import { memberRows } from "../../src/team/rows";
import { unwrap } from "../store/helpers";
import { assertTeamReplays, query, reading } from "./kit";
import { call, memberEvents, receipts, say, start } from "./run-kit";

// mail.claim and its expiry (design §4.6): a worker that claimed a member's pending mail and died
// holds it for the claim TTL; no other worker wakes the member for it until then, and afterwards
// exactly one consumes it.

const Claim = z.array(z.object({ claim_token: z.string().nullable() }));

/** A scripted model whose `nth` request (from 1) waits for `open`. */
function waitsOn(
  nth: number,
  responses: readonly unknown[],
  open: Promise<void>,
): Model {
  const model = scriptedModel({ responses: [...responses] });
  let n = 0;
  const made: Model = {
    ...model,
    send: async function* (...args: Parameters<Model["send"]>) {
      n += 1;
      if (n === nth) await open;
      yield* model.send(...args);
    },
  };
  markTestKit(made);
  return made;
}

describe("mail claims", () => {
  test("a dead worker's claim holds the mail for its TTL; then one worker consumes it once", async () => {
    const db = openBunSqlite(":memory:");
    const artifacts = memoryArtifacts();
    const clock = { now: Date.now() };
    const now = (): number => clock.now;
    const store = storeOf(
      { log: unwrap(await LogStore.open(db, now, artifacts)), artifacts },
      { db, now },
    );
    const researcher = () =>
      agent({
        name: "researcher",
        model: scriptedModel({ responses: [say("Done."), say("Read it.")] }),
      });
    // Run 1: the researcher does its task and goes idle.
    const first = await agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          start("c1", "researcher", "Go."),
          say("Started."),
          say("Reported."),
        ],
      }),
      team: [researcher()],
    }).run("Work.", { store });
    const { thread } = first;
    // Run 2: another executor holds the researcher's lease, so this run's worker claims the
    // message it sends, can't run the member, and stops with the claim still live, as a worker
    // that dies holding it would.
    const log = unwrap(await LogStore.open(db, now, artifacts));
    const row = (
      await reading(db, (tx) => memberRows(tx, first.team.ref.id))
    ).find((m) => m.role === "member");
    if (row?.branch_id === null || row === undefined)
      throw new Error("materialized");
    const holder = unwrap(
      await log.acquire(BranchId.parse(row.branch_id), "elsewhere"),
    );
    const claimed = Promise.withResolvers<void>();
    const watch = setInterval(async () => {
      const rows = Claim.parse(
        await query(
          db,
          "SELECT claim_token FROM mail WHERE kind = 'message'",
          [],
        ),
      );
      if (rows[0]?.claim_token !== null && rows[0] !== undefined)
        claimed.resolve();
    }, 5);
    await agent({
      name: "lead",
      model: waitsOn(
        2,
        [
          call("c2", "send", { to: "researcher-1", text: "More." }),
          say("Sent."),
        ],
        claimed.promise,
      ),
      team: [researcher()],
    }).run("One more thing.", { store, thread });
    clearInterval(watch);
    await holder.release();
    const lead = (responses: readonly unknown[]) =>
      agent({
        name: "lead",
        model: scriptedModel({ responses: [...responses] }),
        team: [researcher()],
      });
    // Within the TTL no other worker wakes the member: the message stays pending.
    await lead([say("Waiting.")]).run("Anything?", { store, thread });
    const team = first.team.ref.id;
    expect(
      receipts(await memberEvents(store, team, "researcher-1"), "message"),
    ).toHaveLength(0);
    // Past the TTL the next worker takes the claim over, and the member consumes it once.
    clock.now += TEAM_CONSTANTS.claimTtlMs + 1;
    await lead([say("Checking.")]).run("Now?", { store, thread });
    await lead([say("Again.")]).run("And now?", { store, thread });
    expect(
      receipts(await memberEvents(store, team, "researcher-1"), "message"),
    ).toHaveLength(1);
    await assertTeamReplays(
      unwrap(await LogStore.open(db, now, artifacts)),
      team,
    );
  });
});
