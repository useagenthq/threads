import { describe, expect, test } from "bun:test";
import { agent, ConfigError, runEvals, sqlite } from "../../src";
import { openStore, tenantStore } from "../../src/agent/sqlite";
import { judgeInput } from "../../src/evals/judge";
import { type KnownEvent, ThreadId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { openThread } from "../../src/thread";
import {
  casesDir,
  issueRefund,
  lookupOrder,
  priced,
  REFUND_TURN,
  refunds,
  saveRefund,
  say,
  support,
  use,
  verdictsReply,
} from "./kit";

// The live check (spec lane 22, C, tests 6-7): the current agent answers the case input, a judge
// grades the whole turn. Every model here is scripted; the global guard is on.

const RUBRIC = [
  "Quotes the 30-day refund window",
  "Looks up the order before refunding",
];
const BUDGET = { max_model_requests: 10 };

async function eventsOf(
  store: ReturnType<typeof sqlite>,
  id: string,
): Promise<readonly KnownEvent[]> {
  const thread = await openThread(store, ThreadId.parse(id));
  if (!thread.ok) throw new Error(thread.error.message);
  const { log } = await openStore(store);
  const read = await log.read(thread.value.branch);
  return read.ok ? knownEvents(read.value) : [];
}

describe("runEvals live", () => {
  test("a scripted judge grades the whole turn; its input is the canonical transcript", async () => {
    const dir = casesDir();
    await saveRefund(dir, "refund-policy", RUBRIC);
    const store = sqlite(":memory:");
    const report = await runEvals({
      cases: dir,
      agents: [support(REFUND_TURN)],
      live: { judge: priced([verdictsReply([true, true])]), budget: BUDGET },
      store,
    });
    const graded = report.cases[0];
    expect(graded?.status).toBe("passed");
    const judge = graded?.checks.judge;
    expect(judge?.verdicts.map((v) => [v.criterion, v.text, v.pass])).toEqual([
      [1, "Quotes the 30-day refund window", true],
      [2, "Looks up the order before refunding", true],
    ]);
    expect(judge?.score).toBe(1);
    expect(report.model_calls).toEqual({ agent: 3, judge: 1 });
    const evals = tenantStore(store, "evals");
    const lead = await eventsOf(evals, judge?.thread_id ?? "");
    const judged = await eventsOf(evals, judge?.judge_thread_id ?? "");
    const asked = judged.find((e) => e.type === "user_input");
    expect(asked?.type === "user_input" ? asked.data.text : "").toBe(
      judgeInput({
        task: "Please refund order 42.",
        events: lead,
        answer: "Refunded order 42; it is inside the 30-day window.",
        rubric: RUBRIC,
      }),
    );
    expect(report.summary).toMatch(
      /^1 passed, 0 failed; 4 model calls \(3 agent, 1 judge\), /,
    );
  });

  test("one failing criterion fails the case; the default store keeps no thread ids", async () => {
    const dir = casesDir();
    await saveRefund(dir, "refund-policy", RUBRIC);
    const report = await runEvals({
      cases: dir,
      agents: [support(REFUND_TURN)],
      live: { judge: priced([verdictsReply([true, false])]), budget: BUDGET },
    });
    const c = report.cases[0];
    expect([c?.status, c?.reason]).toEqual([
      "failed",
      "judge: criterion 2 failed",
    ]);
    expect(c?.checks.judge?.score).toBe(0.5);
    expect(c?.checks.judge?.thread_id).toBeUndefined();
    expect(report.ok).toBe(false);
  });

  test("invalid verdicts twice are judge_invalid, never a pass", async () => {
    const dir = casesDir();
    await saveRefund(dir, "refund-policy", RUBRIC);
    const bad = use(
      "final_output",
      { verdicts: [{ criterion: 1, pass: true, reason: "ok" }] },
      "v1",
    );
    const report = await runEvals({
      cases: dir,
      agents: [support(REFUND_TURN)],
      live: { judge: priced([bad, { ...bad }]), budget: BUDGET },
    });
    expect([report.cases[0]?.status, report.cases[0]?.reason]).toEqual([
      "error",
      "judge_invalid",
    ]);
  });

  test("a case with no criteria is skipped as no_rubric", async () => {
    const dir = casesDir();
    await saveRefund(dir);
    const report = await runEvals({
      cases: dir,
      agents: [support(REFUND_TURN)],
      live: { judge: priced([]), budget: BUDGET },
    });
    expect([report.cases[0]?.status, report.cases[0]?.reason]).toEqual([
      "skipped",
      "no_rubric",
    ]);
    expect(report.model_calls).toEqual({ agent: 0, judge: 0 });
  });

  test("an effect with unrecorded arguments fails closed; nothing is refunded", async () => {
    const dir = casesDir();
    await saveRefund(dir, "refund-policy", RUBRIC);
    const before = refunds.count;
    const other = [
      use("lookup_order", { id: "42" }, "c1"),
      use("issue_refund", { id: "43" }, "c2"),
      say("Done."),
    ];
    const report = await runEvals({
      cases: dir,
      agents: [support(other)],
      live: { judge: priced([]), budget: BUDGET },
    });
    expect(refunds.count).toBe(before);
    expect([report.cases[0]?.status, report.cases[0]?.reason]).toEqual([
      "error",
      "failed: unmatched_external_op",
    ]);
  });

  test("an approval parks the run: error parked", async () => {
    const dir = casesDir();
    await saveRefund(dir, "refund-policy", RUBRIC);
    const asking = agent({
      name: "support",
      model: priced(REFUND_TURN),
      tools: [lookupOrder, issueRefund],
      permissions: { allow: ["lookup_order"], ask: ["issue_refund"] },
    });
    const report = await runEvals({
      cases: dir,
      agents: [asking],
      live: { judge: priced([]), budget: BUDGET },
    });
    expect([report.cases[0]?.status, report.cases[0]?.reason]).toEqual([
      "error",
      "parked",
    ]);
  });

  test("the budget stops a run: error budget_exhausted", async () => {
    const dir = casesDir();
    await saveRefund(dir, "refund-policy", RUBRIC);
    const report = await runEvals({
      cases: dir,
      agents: [support(REFUND_TURN)],
      live: { judge: priced([]), budget: { max_model_requests: 1 } },
    });
    expect([report.cases[0]?.status, report.cases[0]?.reason]).toEqual([
      "error",
      "budget_exhausted",
    ]);
  });

  test("live without a judge, a budget or agents names what is missing", async () => {
    const dir = casesDir();
    const judge = priced([]);
    const refused = async (
      live: object,
      agents: readonly ReturnType<typeof support>[],
    ) => {
      try {
        // @ts-expect-error the options a JavaScript caller could pass without a judge or budget
        await runEvals({ cases: dir, agents, live });
        return "ran";
      } catch (error) {
        return error instanceof ConfigError ? error.message : String(error);
      }
    };
    expect(await refused({ judge }, [support([])])).toBe(
      "live evals need a judge model and a budget: live.budget",
    );
    expect(await refused({ budget: BUDGET }, [support([])])).toBe(
      "live evals need a judge model and a budget: live.judge",
    );
    expect(await refused({ judge, budget: BUDGET }, [])).toBe(
      "live evals need agents: pass agents",
    );
  });
});
