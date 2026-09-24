import { expect, test } from "bun:test";
import { agent, type Model, scriptedModel, sqlite } from "../../src";
import { memberEntry } from "../../src/agent/registry";
import { markTestKit } from "../../src/model/guard";
import { call, say, start } from "./run-kit";

// A member run that fails with a bug while the lead is parked on its members (21E.1 re-review
// F2): the lead waits on the team's progress, which rejects with the failure, so lead.run throws
// it instead of returning parked.

test("a member run's bug while the lead is parked on members reaches lead.run", async () => {
  const researcher = agent({
    name: "researcher",
    model: scriptedModel({ responses: [say("Done."), say("x")] }),
  });
  const writer = agent({
    name: "writer",
    model: scriptedModel({
      responses: [
        call("w1", "ask", { to: "researcher-1", question: "?" }),
        say("Report."),
      ],
    }),
  });
  const base = scriptedModel({
    responses: [
      start("c1", "researcher", "Go."),
      start("c2", "writer", "Ask."),
      say("Started."),
      ...Array.from({ length: 6 }, () => say("Final.")),
    ],
  });
  let requests = 0;
  // The lead's third request waits, so the member's failure lands while it is parked.
  const lead: Model = {
    ...base,
    send: async function* (...args: Parameters<Model["send"]>) {
      requests += 1;
      if (requests === 3) await new Promise((r) => setTimeout(r, 800));
      yield* base.send(...args);
    },
  };
  markTestKit(lead);
  const entry = memberEntry(researcher);
  if (entry === undefined) throw new Error("agent() registers a member entry");
  const run = entry.run;
  let runs = 0;
  Object.defineProperty(entry, "run", {
    value: async (env: Parameters<typeof run>[0]) => {
      runs += 1;
      if (runs >= 2) throw new Error("member bug");
      return run(env);
    },
  });
  const team = agent({ name: "lead", model: lead, team: [researcher, writer] });
  await expect(team.run("Go.", { store: sqlite(":memory:") })).rejects.toThrow(
    "member bug",
  );
}, 15_000);
