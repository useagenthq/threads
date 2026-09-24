import { describe, expect, test } from "bun:test";
import { z } from "zod";
import {
  type AgentOptions,
  agent,
  runEvals,
  scriptedModel,
  tool,
} from "../../src";
import {
  casesDir,
  issueRefund,
  lookupOrder,
  REFUND_TURN,
  saveRefund,
  support,
} from "./kit";

// Drift against real agents (spec lane 22, B.3, test 5): the case's recorded config and the
// agent as it is pinned now, for free: no model call.

const exchange = tool({
  name: "issue_exchange",
  description: "Exchange an order.",
  input: z.object({ id: z.string() }),
  effect: "unguarded",
  execute: async ({ id }) => `exchanged ${id}`,
});

function changed(o: Partial<Omit<AgentOptions<undefined, string>, "output">>) {
  return agent({
    name: "support",
    instructions: "You handle refunds.",
    model: scriptedModel({ responses: [] }),
    tools: [lookupOrder, issueRefund],
    permissions: { allow: ["lookup_order", "issue_refund"] },
    ...o,
  });
}

async function driftOf(bot: ReturnType<typeof support>, strict = false) {
  const dir = casesDir();
  await saveRefund(dir);
  const report = await runEvals({ cases: dir, agents: [bot], strict });
  return { report, c: report.cases[0] };
}

describe("drift", () => {
  test("the recorded config: no drift, and no model call", async () => {
    const same = support(REFUND_TURN);
    const { report, c } = await driftOf(same);
    expect([c?.status, c?.checks.drift]).toEqual([
      "passed",
      { ok: true, kinds: [] },
    ]);
    expect(report.summary).toBe("1 passed, 0 failed");
    expect(report.model_calls).toEqual({ agent: 0, judge: 0 });
  });

  test("changed instructions are prompt drift: stale, which fails only under strict", async () => {
    const bot = changed({ instructions: "You handle refunds and exchanges." });
    const { report, c } = await driftOf(bot);
    expect([c?.status, c?.reason]).toEqual(["stale", "drift: prompt"]);
    expect(report.ok).toBe(true);
    expect((await driftOf(bot, true)).report.ok).toBe(false);
  });

  test("an added, a removed and a reshaped tool are tools drift, by name", async () => {
    const reshaped = tool({
      name: "lookup_order",
      description: "Look up an order by its id.",
      input: z.object({ id: z.string() }),
      effect: "read_only",
      execute: async () => "",
    });
    const { c } = await driftOf(changed({ tools: [reshaped, exchange] }));
    expect(c?.checks.drift).toEqual({
      ok: false,
      kinds: ["tools"],
      tools: {
        added: ["issue_exchange"],
        removed: ["issue_refund"],
        changed: ["lookup_order"],
      },
    });
    expect(c?.reason).toBe(
      "drift: tools (+issue_exchange, -issue_refund, ~lookup_order)",
    );
  });

  test("another model is model drift", async () => {
    const other = scriptedModel({ responses: [] });
    const renamed = {
      ...other,
      info: { ...other.info, params: { max_tokens: 2048 } },
    };
    const { c } = await driftOf(changed({ model: renamed }));
    expect(c?.checks.drift?.kinds).toEqual(["model"]);
  });

  test("a hashed-only change (a concurrent tool) is config drift", async () => {
    const concurrent = tool({
      name: "lookup_order",
      description: "Look up an order by id.",
      input: z.object({ id: z.string() }),
      effect: "read_only",
      concurrent: true,
      execute: async () => "",
    });
    const { c } = await driftOf(changed({ tools: [concurrent, issueRefund] }));
    expect([c?.status, c?.checks.drift?.kinds]).toEqual(["stale", ["config"]]);
  });

  test("an agent the case names is missing: stale, listing the names given", async () => {
    const { c } = await driftOf(changed({ name: "billing" }));
    expect([c?.status, c?.reason, c?.checks.drift?.agents]).toEqual([
      "stale",
      "agent_not_found",
      ["billing"],
    ]);
  });
});
