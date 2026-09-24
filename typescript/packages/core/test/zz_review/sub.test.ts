import { expect, test } from "bun:test";
import { agent, scriptedModel, sqlite } from "../../src";
import { call, logOf, say, start } from "../team/run-kit";

test("a member's subagent is charged to the run budget", async () => {
  const store = sqlite(":memory:");
  const helper = agent({ name: "helper", model: scriptedModel({ responses: [say("h")] }) });
  const researcher = agent({
    name: "researcher",
    model: scriptedModel({ responses: [call("s1", "spawn_agent", { agent: "helper", prompt: "Help." }), say("done")] }),
    subagents: [helper],
  });
  const lead = agent({
    name: "lead",
    model: scriptedModel({ responses: [start("c1", "researcher", "Go."), say("Started."), say("Reported.")] }),
    team: [researcher],
  });
  const r = await lead.run("Work.", { store, budget: { max_model_requests: 5 } });
  console.log(r.status);
  const log = await logOf(store);
  const rows = log.driver.all("SELECT budget_id, attempt_key FROM budget_ledger WHERE limit_name='max_model_requests' ORDER BY attempt_key", []);
  console.log(JSON.stringify(rows, null, 1));
  const reqs = log.driver.all("SELECT branch_id, count(*) n FROM events WHERE type = ? GROUP BY branch_id", ["model_request"]);
  console.log(JSON.stringify(reqs));
});
