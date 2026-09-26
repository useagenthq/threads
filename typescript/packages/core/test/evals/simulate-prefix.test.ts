import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import {
  type Agent,
  agent,
  openThread,
  runEvals,
  scriptedModel,
  type Tool,
  tool,
} from "../../src";
import { tenantStore } from "../../src/agent/sqlite";
import { ThreadId } from "../../src/log";
import {
  casesDir,
  memory,
  priced,
  saveTurns,
  say,
  use,
  verdictsReply,
} from "./kit";

// The prefix of a simulated case (spec lane 32, B.1, tests 4 and 5): continued when the agent's
// config_hash is unchanged (the real earlier turns are imported and the thread goes on, and the
// live stub queue starts at the saved turn's entries), re-driven when it changed.

const RUBRIC = ["The agent stays inside the refund policy"];
const BUDGET = { max_model_requests: 40 };
const SCRIPT = { kind: "script", messages: ["Thanks."] } as const;

/** Every refund this process really issued, and a distinct result per call. */
const issued: { count: number } = { count: 0 };

const countingRefund: Tool<{ id: string }, string> = tool({
  name: "issue_refund",
  description: "Refund an order.",
  input: z.object({ id: z.string() }),
  runs: "host",
  effect: "unguarded",
  execute: async ({ id }) => {
    issued.count += 1;
    return `refund #${issued.count} for ${id}`;
  },
});

function refunder(
  responses: readonly unknown[],
  instructions = "You handle refunds.",
): Agent {
  return agent({
    name: "support",
    instructions,
    model: scriptedModel({ responses }),
    tools: [countingRefund],
    permissions: { allow: ["issue_refund"] },
  });
}

/** Two recorded turns, each refunding order 42: the same call, two different results. */
const RECORDED = [
  use("issue_refund", { id: "42" }, "p1"),
  say("Refunded it the first time."),
  use("issue_refund", { id: "42" }, "c1"),
  say("Refunded it again."),
];

const SAVE = {
  rubric: RUBRIC,
  simulate: SCRIPT,
  must: { type: "tool_call" as const, data: { name: "issue_refund" } },
};

async function texts(store: ReturnType<typeof memory>, id: string) {
  const opened = await openThread(
    tenantStore(store, "evals"),
    ThreadId.parse(id),
  );
  if (!opened.ok) throw new Error("the live thread opens");
  const timeline = await opened.value.timeline();
  if (!timeline.ok) throw new Error("the live thread reads back");
  const events = timeline.value.entries.map((e) => e.event);
  return {
    inputs: events.flatMap((e) =>
      e.type === "user_input" && e.data.text !== undefined ? [e.data.text] : [],
    ),
    results: events.flatMap((e) =>
      e.type === "tool_result" ? [e.data.preview] : [],
    ),
    requests: events.filter((e) => e.type === "model_request").length,
  };
}

describe("a continued prefix", () => {
  test("imports the real log and answers from the saved turn's stub, not the prefix's", async () => {
    const dir = casesDir();
    issued.count = 0;
    const path = await saveTurns(
      dir,
      "continued",
      refunder(RECORDED),
      ["Refund order 42.", "Do it once more."],
      SAVE,
    );
    const stubs = JSON.parse(readFileSync(join(path, "stubs.json"), "utf8"));
    const hash: string = stubs.stubs[0].args_hash;
    // One key, two entries, numbered in log order: the prefix's first.
    expect(stubs.stubs).toEqual([
      {
        tool: "issue_refund",
        args_hash: hash,
        occurrence: 0,
        output: "refund #1 for 42",
        is_error: false,
        scope: "prefix",
      },
      {
        tool: "issue_refund",
        args_hash: hash,
        occurrence: 1,
        output: "refund #2 for 42",
        is_error: false,
      },
    ]);
    issued.count = 0;
    const store = memory();
    const report = await runEvals({
      cases: dir,
      agents: [
        refunder([
          use("issue_refund", { id: "42" }, "q1"),
          say("Refunded it again."),
          say("You're welcome."),
        ]),
      ],
      store,
      live: {
        judge: priced([verdictsReply([true])], "judge-model"),
        budget: BUDGET,
      },
    });
    const [only] = report.cases;
    expect(only?.simulation).toMatchObject({
      prefix: "continued",
      prefix_turns: 1,
      messages: 2,
      ended: "script_done",
    });
    expect(only?.status).toBe("passed");
    expect(issued.count).toBe(0);
    const seen = await texts(store, only?.checks.judge?.thread_id ?? "");
    // The two real inputs came from the import; only the opener and the script message ran.
    expect(seen.inputs).toEqual([
      "Refund order 42.",
      "Do it once more.",
      "Thanks.",
    ]);
    // The prefix's own result is in the imported log; the live call took the saved turn's.
    expect(seen.results).toEqual(["refund #1 for 42", "refund #2 for 42"]);
    // The imported prefix holds 2 requests; this conversation added 3.
    expect(report.model_calls.agent).toBe(3);
    expect(seen.requests).toBe(5);
  });
});

describe("a re-driven prefix", () => {
  test("sends the recorded inputs on a new thread and answers the prefix effect from its stub", async () => {
    const dir = casesDir();
    issued.count = 0;
    await saveTurns(
      dir,
      "redriven",
      refunder(RECORDED, "You handled refunds, once."),
      ["Refund order 42.", "Do it once more."],
      SAVE,
    );
    issued.count = 0;
    const store = memory();
    const report = await runEvals({
      cases: dir,
      // Different instructions: a different config_hash, so the prefix is re-driven.
      agents: [
        refunder(
          [...RECORDED, say("You're welcome.")],
          "You handle refunds now.",
        ),
      ],
      store,
      live: {
        judge: priced([verdictsReply([true])], "judge-model"),
        budget: BUDGET,
      },
    });
    const [only] = report.cases;
    // The changed instructions are real drift, so the judged pass reads stale.
    expect([only?.status, only?.reason]).toEqual(["stale", "drift: prompt"]);
    expect(only?.simulation).toMatchObject({
      prefix: "redriven",
      prefix_turns: 1,
      messages: 2,
      ended: "script_done",
    });
    // Both refunds answered from stubs: the effect never ran again.
    expect(issued.count).toBe(0);
    const seen = await texts(store, only?.checks.judge?.thread_id ?? "");
    expect(seen.inputs).toEqual([
      "Refund order 42.",
      "Do it once more.",
      "Thanks.",
    ]);
    // The queue starts at the prefix entry, so the re-driven turn gets it.
    expect(seen.results).toEqual(["refund #1 for 42", "refund #2 for 42"]);
  });
});
