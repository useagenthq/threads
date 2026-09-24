import { describe, expect, test } from "bun:test";
import {
  agent,
  type Model,
  ModelBlockedError,
  runEvals,
  scriptedModel,
} from "../../src";
import {
  casesDir,
  REFUND_TURN,
  saveRefund,
  say,
  support,
  use,
  verdictsReply,
} from "./kit";

// The model-request guard as a value (spec lane 22, C.7, tests 4b and 8): a live model the guard
// blocks stops the run, which returns the report with `aborted`; no request is sent. Any other
// throw is a bug and leaves runEvals.

const RUBRIC = ["Quotes the 30-day refund window"];
const BUDGET = { max_model_requests: 10 };

/** A model that is not test kit, counting the requests that reach it (none may). */
function real(responses: readonly unknown[]): Model & { sent: number } {
  const inner = scriptedModel({ responses });
  const model = {
    info: inner.info,
    sent: 0,
    send: (...args: Parameters<Model["send"]>) => {
      model.sent += 1;
      return inner.send(...args);
    },
  };
  return model;
}

describe("the guard stops a live eval", () => {
  test("ModelBlockedError is a typed Error and names the model", () => {
    const e = new ModelBlockedError("anthropic/claude-haiku-4-5");
    expect(e).toBeInstanceOf(Error);
    expect(e.model).toBe("anthropic/claude-haiku-4-5");
  });

  test("a blocked judge: that case errors, the rest are not_run, nothing is sent", async () => {
    const dir = casesDir();
    await saveRefund(dir, "a-refund", RUBRIC);
    await saveRefund(dir, "b-refund", RUBRIC);
    const judge = real([verdictsReply([true])]);
    const report = await runEvals({
      cases: dir,
      agents: [support([...REFUND_TURN, ...REFUND_TURN])],
      live: { judge, budget: BUDGET },
    });
    expect(report.aborted).toEqual({
      code: "model_blocked",
      case: "a-refund",
      model: "scripted/scripted-1",
    });
    expect(report.cases.map((c) => [c.name, c.status, c.reason])).toEqual([
      ["a-refund", "error", "model_blocked"],
      ["b-refund", "not_run", undefined],
    ]);
    expect(judge.sent).toBe(0);
    expect(report.ok).toBe(false);
  });

  test("a blocked model on a background subagent aborts the eval, not just the child", async () => {
    const dir = casesDir();
    await saveRefund(dir, "refund-policy", RUBRIC);
    const child = real([say("never")]);
    const reviewer = agent({ name: "reviewer", model: child });
    const spawn = use(
      "spawn_agent",
      { agent: "reviewer", prompt: "Check.", background: true },
      "c0",
    );
    const lead = agent({
      name: "support",
      model: scriptedModel({ responses: [spawn, say("Started a review.")] }),
      subagents: [reviewer],
    });
    const report = await runEvals({
      cases: dir,
      agents: [lead],
      live: { judge: scriptedModel({ responses: [] }), budget: BUDGET },
    });
    expect(report.aborted?.code).toBe("model_blocked");
    expect(child.sent).toBe(0);
  });

  test("a bug in the runner's own inputs propagates out of runEvals", async () => {
    const dir = casesDir();
    await saveRefund(dir, "refund-policy", RUBRIC);
    const live = {
      judge: scriptedModel({ responses: [] }),
      budget: BUDGET,
      get rubric(): readonly string[] {
        throw new TypeError("a broken test double");
      },
    };
    await expect(
      runEvals({ cases: dir, agents: [support(REFUND_TURN)], live }),
    ).rejects.toThrow("a broken test double");
  });
});
