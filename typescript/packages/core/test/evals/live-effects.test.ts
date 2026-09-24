import { describe, expect, test } from "bun:test";
import { agent, fakeSandbox, runEvals, scriptedModel } from "../../src";
import {
  casesDir,
  issueRefund,
  lookupOrder,
  REFUND_TURN,
  refunds,
  saveRefund,
  say,
  use,
} from "./kit";

// A live eval never performs a real side effect (spec lane 22, C.1; review H1 and H2): every
// effectful call of the run tree answers from the case's recordings, a handoff target's too, and
// sandbox tools run for real only inside a sandbox whose egress is deny-all.

const RUBRIC = ["Quotes the 30-day refund window"];
const BUDGET = { max_model_requests: 10 };

describe("live eval side effects", () => {
  test("a handoff target's effects answer from the recordings: nothing is refunded", async () => {
    const dir = casesDir();
    await saveRefund(dir, "refund-policy", RUBRIC);
    const billing = agent({
      name: "billing",
      model: scriptedModel({
        responses: [use("issue_refund", { id: "99" }, "b1"), say("Refunded.")],
      }),
      tools: [issueRefund],
      permissions: { allow: ["issue_refund"] },
    });
    const support = agent({
      name: "support",
      instructions: "You handle refunds.",
      model: scriptedModel({
        responses: [use("handoff", { agent: "billing" }, "h1")],
      }),
      tools: [lookupOrder, issueRefund],
      permissions: { allow: ["lookup_order", "issue_refund"] },
      handoffs: [billing],
    });
    const before = refunds.count;
    const report = await runEvals({
      cases: dir,
      agents: [support],
      live: { judge: scriptedModel({ responses: [] }), budget: BUDGET },
    });
    expect(refunds.count).toBe(before);
    expect(report.cases[0]?.status).toBe("error");
  });

  test("a team lead is never run live: its members run where the stubs can't reach", async () => {
    const dir = casesDir();
    await saveRefund(dir, "refund-policy", RUBRIC);
    const billing = agent({
      name: "billing",
      model: scriptedModel({ responses: [] }),
      tools: [issueRefund],
    });
    const lead = agent({
      name: "support",
      instructions: "You handle refunds.",
      model: scriptedModel({ responses: REFUND_TURN }),
      tools: [lookupOrder, issueRefund],
      team: [billing],
    });
    const report = await runEvals({
      cases: dir,
      agents: [lead],
      live: { judge: scriptedModel({ responses: [] }), budget: BUDGET },
    });
    expect([report.cases[0]?.status, report.cases[0]?.reason]).toEqual([
      "skipped",
      "live_not_runnable:team_calls",
    ]);
    expect(report.model_calls).toEqual({ agent: 0, judge: 0 });
  });

  const bashTurn = [use("bash", { command: "echo hi" }, "s1"), ...REFUND_TURN];

  test("with deny-all egress, sandbox tools run for real in the eval's sandbox", async () => {
    const dir = casesDir();
    await saveRefund(dir, "refund-policy", RUBRIC);
    const sandbox = fakeSandbox();
    const bot = agent({
      name: "support",
      instructions: "You handle refunds.",
      model: scriptedModel({ responses: bashTurn }),
      tools: [lookupOrder, issueRefund],
      permissions: { allow: ["lookup_order", "issue_refund", "bash"] },
      sandbox,
    });
    const report = await runEvals({
      cases: dir,
      agents: [bot],
      live: { judge: scriptedModel({ responses: [] }), budget: BUDGET },
    });
    // The judge has no reply scripted: the agent's run completed, bash included.
    expect(report.cases[0]?.reason).toBe("judge_invalid");
    expect(
      sandbox.execs().some((e) => e.command.join(" ").includes("echo hi")),
    ).toBe(true);
  });

  test("without deny-all egress, a sandbox tool fails closed: unmatched_external_op", async () => {
    const dir = casesDir();
    await saveRefund(dir, "refund-policy", RUBRIC);
    const fake = fakeSandbox();
    const open = {
      ...fake,
      info: { ...fake.info, egress: "unenforced" as const },
    };
    const bot = agent({
      name: "support",
      instructions: "You handle refunds.",
      model: scriptedModel({ responses: bashTurn }),
      tools: [lookupOrder, issueRefund],
      permissions: { allow: ["lookup_order", "issue_refund", "bash"] },
      sandbox: open,
      egress: "unenforced",
    });
    const report = await runEvals({
      cases: dir,
      agents: [bot],
      live: { judge: scriptedModel({ responses: [] }), budget: BUDGET },
    });
    expect([report.cases[0]?.status, report.cases[0]?.reason]).toEqual([
      "error",
      "failed: unmatched_external_op",
    ]);
    expect(fake.execs()).toEqual([]);
  });
});
