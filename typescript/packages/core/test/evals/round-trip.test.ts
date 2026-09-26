import { describe, expect, test } from "bun:test";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { runEvals } from "../../src";
import { unwrap } from "../store/helpers";
import { casesDir, memory, REFUND_TURN, refunds, support } from "./kit";

// spec lane 22, tests 1-3: a scripted thread's first turn saved without a snapshot or a sandbox,
// then run by runEvals offline: zero model calls, no effect, and a changed recorded result fails.

async function saved(dir: string) {
  const bot = support(REFUND_TURN);
  const run = await bot.run("Please refund order 42.", { store: memory() });
  expect(run.status).toBe("completed");
  return unwrap(
    await run.thread.saveCase("refund-policy", {
      expect: { must: [{ type: "tool_call", data: { name: "lookup_order" } }] },
      externalEffects: "stub",
      rubric: ["Quotes the 30-day refund window"],
      dir,
    }),
  );
}

describe("saveCase then runEvals", () => {
  test("a first turn with no sandbox saves portable and passes offline", async () => {
    const dir = casesDir();
    const case_ = await saved(dir);
    expect(case_).toEqual({ path: join(dir, "refund-policy"), portable: true });
    const before = refunds.count;
    const report = await runEvals({ cases: dir });
    expect(refunds.count).toBe(before);
    expect(report.cases).toEqual([
      {
        name: "refund-policy",
        status: "passed",
        checks: {
          replay: { ok: true },
          rerun: {
            ok: true,
            unmatched: [],
            script_left: 0,
            stubs_unmatched: 0,
            unrecorded_calls: 0,
            unrecorded_hooks: 0,
          },
        },
      },
    ]);
    expect(report.ok).toBe(true);
    expect(report.summary).toBe(
      "1 passed, 0 failed (framework checks only; pass --agent to detect changes to your agents)",
    );
    expect(report.model_calls).toEqual({ agent: 0, user: 0, judge: 0 });
  });

  test("a changed read-only result in sandbox.json fails the rerun: it really re-executes", async () => {
    const dir = casesDir();
    await saved(dir);
    const path = join(dir, "refund-policy", "sandbox.json");
    const sandbox = JSON.parse(readFileSync(path, "utf8"));
    sandbox.results[0].preview = "order 42: never shipped";
    writeFileSync(path, JSON.stringify(sandbox));
    const report = await runEvals({ cases: dir });
    expect(report.cases[0]?.status).toBe("failed");
    expect(report.cases[0]?.checks.rerun?.mismatch).toEqual({
      index: 5,
      want: "tool_result",
      got: "tool_result",
    });
    expect(report.ok).toBe(false);
  });

  test("a simulated case saved elsewhere reruns offline here, with no prefix stub left over", async () => {
    // Cross-language (spec lane 32, test 11): the corpus's bytes are the shared contract.
    const evals = join(
      import.meta.dir,
      "../../../../../spec/conformance/evals",
    );
    for (const name of [
      "eval-simulate-offline",
      "eval-simulate-prefix-stubs",
    ]) {
      const report = await runEvals({ cases: join(evals, name, "cases") });
      expect(report.cases.map((c) => c.status)).toEqual(
        report.cases.map(() => "passed"),
      );
    }
  });
});
