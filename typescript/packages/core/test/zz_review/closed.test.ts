import { test } from "bun:test";
import { agent, type Model, scriptedModel, sqlite } from "../../src";
import { markTestKit } from "../../src/model/guard";
import { logOf, say, start } from "../team/run-kit";

test("a member runs after its lead closed", async () => {
  const store = sqlite(":memory:");
  const log = await logOf(store);
  const seen: string[] = [];
  const base = scriptedModel({ responses: [say("a"), say("b")] });
  const slow: Model = {
    ...base,
    send: async function* (...args: Parameters<Model["send"]>) {
      seen.push(JSON.stringify(log.driver.all("SELECT closed_at FROM teams", [])));
      await new Promise((r) => setTimeout(r, 50));
      yield* base.send(...args);
    },
  };
  markTestKit(slow);
  const researcher = agent({ name: "researcher", model: slow });
  const lead = agent({
    name: "lead",
    model: scriptedModel({ responses: [start("c1", "researcher", "Go.")] }),
    team: [researcher],
  });
  const t0 = Date.now();
  const timer = setInterval(() => console.log("tick", Date.now() - t0, JSON.stringify(log.driver.all("SELECT name, state FROM team_members", [])), JSON.stringify(log.driver.all("SELECT kind, state FROM mail", []))), 2000);
  const r = await lead.run("Work.", { store });
  clearInterval(timer);
  console.log(r.status, JSON.stringify(r.status === "failed" ? r.error : ""), Date.now() - t0, "ms");
  console.log(seen);
  console.log(JSON.stringify(log.driver.all("SELECT name, state FROM team_members", [])));
  console.log(JSON.stringify(log.driver.all("SELECT kind, state FROM mail", [])));
});
