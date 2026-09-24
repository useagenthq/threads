import { expect, test } from "bun:test";
import { z } from "zod";
import { agent, scriptedModel } from "../../src";
import { BranchId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { memberRows } from "../../src/team/rows";
import { unwrap } from "../store/helpers";
import { Crash, crashing, drill } from "./crash-kit";
import { assertTeamReplays } from "./kit";
import { answering, askIds, call, replyTo, say, start, types } from "./run-kit";

// An asker that ends (spec/schema/README.md, "Teams", "An asker that ends"): the writer parks on
// its ask, the process dies as the researcher replies, and on the restart the writer's rebind
// fails (its definition changed). Its end closes its open ask cancelled, so no asks row outlives
// it, and the researcher's reply is refused one way or the other.

const researcher = () =>
  agent({
    name: "researcher",
    model: answering((request) =>
      askIds(request).length > 0 && !request.includes('\\"name\\":\\"reply\\"')
        ? replyTo("r1", request, "Batteries.")
        : say("Done."),
    ),
  });

const writer = (instructions: string) =>
  agent({
    name: "writer",
    instructions,
    model: scriptedModel({
      responses: [
        call("w1", "ask", { to: "researcher-1", question: "Which topic?" }),
        say("Report."),
      ],
    }),
  });

const lead = (instructions: string) =>
  agent({
    name: "lead",
    model: scriptedModel({
      responses: [
        start("c1", "researcher", "Read."),
        start("c2", "writer", "Ask researcher-1."),
        say("Started."),
        ...Array.from({ length: 6 }, () => say("Final.")),
      ],
    }),
    team: [researcher(), writer(instructions)],
  });

test("an asker whose rebind fails closes its open ask cancelled before its end", async () => {
  const d = drill();
  const reply = {
    name: "the researcher's reply",
    at: (sql: string, params: readonly unknown[]) =>
      sql.includes("INSERT INTO mail") && params[2] === "reply",
  };
  await expect(
    lead("Write.").run("Work.", { store: d.open(crashing(d.db, reply)) }),
  ).rejects.toThrow(Crash);
  const open = () =>
    d.db.all("SELECT ask_id FROM asks WHERE state = 'open'", []).length;
  expect(open()).toBe(1);

  const { team } = await d.restart(lead("Write differently."));
  const row = memberRows(d.db, team).find((r) => r.name === "writer-1");
  const branch = BranchId.parse(z.string().parse(row?.branch_id));
  const log = knownEvents(unwrap(d.log.read(branch)));
  const closed = log.find((e) => e.type === "ask_closed");
  expect(closed?.type === "ask_closed" && closed.data.outcome).toEqual({
    status: "cancelled",
  });
  const at = types(log).indexOf("ask_closed");
  expect(types(log)[at + 1]).toBe("member_ended");
  expect(row?.state).toBe("ended");
  expect(open()).toBe(0);
  assertTeamReplays(d.log, team);
});
