import { describe, expect, test } from "bun:test";
import { runEvals } from "../../src";
import { tenantStore } from "../../src/agent/sqlite";
import { remainingBudget, type Spent } from "../../src/evals/remaining-budget";
import { ThreadId } from "../../src/log";
import { openThread } from "../../src/thread/open";
import {
  casesDir,
  memory,
  priced,
  saveTurns,
  say,
  support,
  use,
  verdictsReply,
} from "./kit";

// One budget per conversation (spec lane 32, B.5, test 6): live.budget covers every agent run and
// every simulated-user run of the case, so each run is given what is left, field by field. A
// field the author set whose remainder is <= 0 ends the case before the next run.

const RUBRIC = ["The agent stays inside the refund policy"];

const NOTHING: Spent = {
  requests: 0,
  inputTokens: 0,
  outputTokens: 0,
  turns: 0,
  costNanos: 0,
  wallMs: 0,
};

/** One turn of two requests: a read-only call, then the answer. */
const turn = (n: number) => [
  use("lookup_order", { id: "42" }, `r${n}`),
  say("Looked it up."),
];

const reads = (turns: number) =>
  support(Array.from({ length: turns }, (_, i) => turn(i)).flat());

async function budgets(store: ReturnType<typeof memory>, id: string) {
  const opened = await openThread(
    tenantStore(store, "evals"),
    ThreadId.parse(id),
  );
  if (!opened.ok) throw new Error("the live thread opens");
  const timeline = await opened.value.timeline();
  if (!timeline.ok) throw new Error("the live thread reads back");
  return timeline.value.entries
    .map((e) => e.event)
    .flatMap((e) => (e.type === "user_input" ? [e.data.budget] : []));
}

describe("the arithmetic", () => {
  test("each field is the limit less what the conversation used", () => {
    const left = remainingBudget(
      { max_model_requests: 4, max_turns: 3, max_wall_ms: 1000 },
      { ...NOTHING, requests: 2, turns: 1, wallMs: 400 },
    );
    expect(left).toEqual({
      ok: true,
      budget: { max_model_requests: 2, max_turns: 2, max_wall_ms: 600 },
    });
  });

  test("a field the author did not set is never passed", () => {
    const left = remainingBudget(
      { max_turns: 2 },
      { ...NOTHING, requests: 99, turns: 1 },
    );
    expect(left).toEqual({ ok: true, budget: { max_turns: 1 } });
  });

  test("a remainder below 1 is exhausted, not a zero limit", () => {
    expect(
      remainingBudget({ max_model_requests: 2 }, { ...NOTHING, requests: 2 }),
    ).toEqual({ ok: false });
  });

  test("a wall clock advanced across both threads counts once", () => {
    // 250 ms on the agent thread and 300 on the user thread: one monotonic clock, 550 spent.
    const left = remainingBudget(
      { max_wall_ms: 1000 },
      { ...NOTHING, wallMs: 250 + 300 },
    );
    expect(left).toEqual({ ok: true, budget: { max_wall_ms: 450 } });
  });

  test("unknown usage counts at its upper bound", () => {
    expect(
      remainingBudget(
        { max_cost_nanos: 1000 },
        { ...NOTHING, costNanos: 1000 },
      ),
    ).toEqual({ ok: false });
  });
});

describe("the conversation's runs", () => {
  test("max_model_requests is spent down and the case ends budget_exhausted", async () => {
    const dir = casesDir();
    await saveTurns(dir, "spend", reads(1), ["Please refund order 42."], {
      rubric: RUBRIC,
      simulate: { kind: "script", messages: ["And again?", "And again?"] },
    });
    const store = memory();
    const report = await runEvals({
      cases: dir,
      agents: [reads(3)],
      store,
      live: {
        judge: priced([verdictsReply([true])], "judge-model"),
        budget: { max_model_requests: 4 },
      },
    });
    const [only] = report.cases;
    expect([only?.status, only?.reason]).toEqual(["error", "budget_exhausted"]);
    // The agent ran twice, two requests each; the third run had nothing left.
    expect(report.model_calls.agent).toBe(4);
  });

  test("each run records the budget it was given", async () => {
    const dir = casesDir();
    await saveTurns(dir, "given", reads(1), ["Please refund order 42."], {
      rubric: RUBRIC,
      simulate: { kind: "script", messages: ["And again?"] },
    });
    const store = memory();
    const report = await runEvals({
      cases: dir,
      agents: [reads(2)],
      store,
      live: {
        judge: priced([verdictsReply([true])], "judge-model"),
        budget: { max_model_requests: 6 },
      },
    });
    const id = report.cases[0]?.checks.judge?.thread_id ?? "";
    // The opener got the whole budget; the next run got what its two requests left.
    expect(await budgets(store, id)).toEqual([
      { max_model_requests: 6 },
      { max_model_requests: 4 },
    ]);
    expect(report.cases[0]?.simulation?.messages).toBe(2);
  });

  test("a turn_completed the agent adds itself is charged to max_turns", async () => {
    const dir = casesDir();
    await saveTurns(dir, "turns", reads(1), ["Please refund order 42."], {
      rubric: RUBRIC,
      simulate: { kind: "script", messages: ["And again?", "Once more?"] },
    });
    const report = await runEvals({
      cases: dir,
      agents: [reads(3)],
      live: {
        judge: priced([verdictsReply([true])], "judge-model"),
        budget: { max_turns: 2 },
      },
    });
    // Two turns fit; the third run is refused before it starts.
    expect([report.cases[0]?.status, report.cases[0]?.reason]).toEqual([
      "error",
      "budget_exhausted",
    ]);
  });
});
