import { expect, test } from "bun:test";
import { agent, runEvals, sqlite } from "../../src";
import { tenantStore } from "../../src/agent/sqlite";
import type { Cost } from "../../src/log";
import { ThreadId } from "../../src/log";
import { openThread } from "../../src/thread";
import {
  casesDir,
  issueRefund,
  lookupOrder,
  priced,
  REFUND_TURN,
  saveRefund,
  say,
  use,
  verdictsReply,
} from "./kit";

// What a live run reports it spent (spec lane 22, C.6): model_calls counts every model_request
// of the live threads, subagents included, and of the judge threads; cost sums
// Thread.cost({tree: true}) over them.

const RUBRIC = ["Quotes the 30-day refund window"];
const BUDGET = { max_model_requests: 10 };

async function treeCost(
  store: ReturnType<typeof sqlite>,
  id: string | undefined,
): Promise<Cost | null> {
  const thread = await openThread(
    tenantStore(store, "evals"),
    ThreadId.parse(id),
  );
  if (!thread.ok) throw new Error(thread.error.message);
  const cost = await thread.value.cost({ tree: true });
  return cost.ok ? cost.value : null;
}

test("model_calls counts a subagent's request, and cost is the sum of the tree costs", async () => {
  const dir = casesDir();
  await saveRefund(dir, "refund-policy", RUBRIC);
  const reviewer = agent({
    name: "reviewer",
    model: priced([say("Looks right.")], "reviewer-model"),
  });
  const spawn = use(
    "spawn_agent",
    { agent: "reviewer", prompt: "Check the refund." },
    "c0",
  );
  const lead = agent({
    name: "support",
    model: priced([spawn, ...REFUND_TURN]),
    tools: [lookupOrder, issueRefund],
    permissions: { allow: ["lookup_order", "issue_refund"] },
    subagents: [reviewer],
  });
  const store = sqlite(":memory:");
  const report = await runEvals({
    cases: dir,
    agents: [lead],
    live: {
      judge: priced([verdictsReply([true])], "judge-model"),
      budget: BUDGET,
    },
    store,
  });
  const judge = report.cases[0]?.checks.judge;
  // The judge passed it; the agent's config changed since the case was saved, so it is stale.
  expect(report.cases[0]?.status).toBe("stale");
  expect(judge?.score).toBe(1);
  expect(report.model_calls).toEqual({ agent: 5, user: 0, judge: 1 });
  const agentCost = await treeCost(store, judge?.thread_id);
  const judgeCost = await treeCost(store, judge?.judge_thread_id);
  expect(report.cost?.known_nanos).toBe(
    (agentCost?.known_nanos ?? -1) + (judgeCost?.known_nanos ?? -1),
  );
  expect(report.cost?.known_nanos).toBeGreaterThan(0);
});
