import { expect, test } from "bun:test";
import { z } from "zod";
import { agent, scriptedModel } from "../../src";
import { BranchId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { memberRows } from "../../src/team/rows";
import { unwrap } from "../store/helpers";
import { Crash, crashing, drill, mentions } from "./crash-kit";
import { assertTeamReplays, reading } from "./kit";
import { answering, call, receipts, say, start, types } from "./run-kit";

// A crash drill at a cancel's application (design §7, Phase 1 proofs): the process dies inside
// the member's append that takes the cancel (its receipt and cancel_requested). Nothing of it is
// stored; on the restart the still-pending cancel wakes the member, which applies it once and
// ends cancelled once.

const lead = (fresh: boolean) =>
  agent({
    name: "lead",
    model: fresh
      ? scriptedModel({
          responses: [
            start("c1", "researcher", "Read."),
            call("c2", "cancel", { member: "researcher-1" }),
            ...Array.from({ length: 4 }, () => say("Final.")),
          ],
        })
      : answering(() => say("Final.")),
    team: [agent({ name: "researcher", model: answering(() => say("Done.")) })],
  });

test("a crash inside a cancel's application stores none of it; the restart applies it once", async () => {
  const d = await drill();
  const point = {
    name: "the member's cancel",
    at: (sql: string, params: readonly unknown[]) =>
      sql.includes("INSERT INTO events") &&
      mentions(
        params.filter((p) => typeof p === "string" || p instanceof Uint8Array),
        "cancel_requested",
      ),
  };
  await expect(
    lead(true).run("Work.", { store: await d.open(crashing(d.db, point)) }),
  ).rejects.toThrow(Crash);
  const { result, team } = await d.restart(lead(false));
  expect(result.status).toBe("completed");
  const row = (await reading(d.db, (tx) => memberRows(tx, team))).find(
    (r) => r.name === "researcher-1",
  );
  const branch = BranchId.parse(z.string().parse(row?.branch_id));
  const member = knownEvents(unwrap(await d.log.read(branch)));
  expect(receipts(member, "cancel")).toHaveLength(1);
  expect(types(member).filter((t) => t === "cancel_requested")).toHaveLength(1);
  const ended = member.filter((e) => e.type === "member_ended");
  expect(ended).toHaveLength(1);
  expect(ended[0]?.type === "member_ended" && ended[0].data.result.status).toBe(
    "cancelled",
  );
  await assertTeamReplays(d.log, team);
});
