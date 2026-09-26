import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  agent,
  openThread,
  runEvals,
  type Simulate,
  scriptedModel,
} from "../../src";
import { tenantStore } from "../../src/agent/sqlite";
import { simulatedUserInput } from "../../src/evals/simulated-user";
import { ThreadId } from "../../src/log";
import {
  casesDir,
  issueRefund,
  lookupOrder,
  memory,
  priced,
  REFUND_TURN,
  refunds,
  saveTurns,
  say,
  support,
  use,
  userReplies,
  verdictsReply,
} from "./kit";

// Simulated users (spec lane 32, tests 1-3, 7 and 8): the live conversation, who plays the user,
// what ends it, and that an effect the recording doesn't have fails the case closed.

const RUBRIC = ["The agent stays inside the refund policy"];
const BUDGET = { max_model_requests: 40 };

const PERSONA =
  "A customer who bought headphones 40 days ago. Polite but persistent.";
const GOAL = "Get a refund, or a clear reason why not.";

const model = (max?: number): Simulate => ({
  kind: "model",
  persona: PERSONA,
  goal: GOAL,
  ...(max === undefined ? {} : { maxMessages: max }),
});

/** One read-only turn: look the order up, then answer. A call id is used once per thread. */
const readTurn = (n: number) => [
  use("lookup_order", { id: "42" }, `r${n}`),
  say("Looked it up."),
];

const reads = (turns: number) =>
  support(Array.from({ length: turns }, (_, i) => readTurn(i)).flat());

async function simulated(
  dir: string,
  name: string,
  simulate: Simulate,
  turns = 1,
): Promise<void> {
  await saveTurns(dir, name, reads(turns), ["Please refund order 42."], {
    rubric: RUBRIC,
    simulate,
    must: { type: "tool_call", data: { name: "lookup_order" } },
  });
}

/** The first user_input of a thread: what the judge or the simulated user was asked. */
async function judgeAsked(
  store: ReturnType<typeof memory>,
  id: string,
): Promise<string> {
  const opened = await openThread(
    tenantStore(store, "evals"),
    ThreadId.parse(id),
  );
  if (!opened.ok) throw new Error("the judge thread is kept");
  const timeline = await opened.value.timeline();
  if (!timeline.ok) throw new Error("the judge thread reads back");
  for (const entry of timeline.value.entries) {
    const e = entry.event;
    if (e.type !== "user_input") continue;
    const { text } = e.data;
    if (typeof text === "string") return text;
  }
  return "";
}

describe("a model plays the user", () => {
  test("done on its fourth reply ends the conversation with four messages", async () => {
    const dir = casesDir();
    await simulated(dir, "pushback", model());
    const store = memory();
    const report = await runEvals({
      cases: dir,
      agents: [reads(4)],
      store,
      live: {
        judge: priced([verdictsReply([true])], "judge-model"),
        user: priced(
          userReplies([
            "What about a repair?",
            "Is there any exception?",
            "Understood.",
            { done: true },
          ]),
          "user-model",
        ),
        budget: BUDGET,
      },
    });
    const [only] = report.cases;
    expect([only?.status, only?.reason]).toEqual(["passed", undefined]);
    expect(only?.simulation).toEqual({
      messages: 4,
      prefix: "none",
      prefix_turns: 0,
      ended: "user_done",
      user_thread_id: only?.simulation?.user_thread_id ?? "",
    });
    expect(report.model_calls.user).toBe(4);
    expect(report.summary).toContain("pushback: 4 messages, user done");
    // The judge sees every user message after the opener, in order (spec lane 32, D).
    const asked = await judgeAsked(
      store,
      only?.checks.judge?.judge_thread_id ?? "",
    );
    const users = JSON.parse(asked).transcript.filter(
      (i: { kind: string }) => i.kind === "user",
    );
    expect(users).toEqual([
      { kind: "user", text: "What about a repair?" },
      { kind: "user", text: "Is there any exception?" },
      { kind: "user", text: "Understood." },
    ]);
    expect(JSON.parse(asked).goal).toBe(GOAL);
  });

  test("its first input is the whole visible conversation, later ones the new reply", async () => {
    const dir = casesDir();
    await simulated(dir, "seen", model(2));
    const store = memory();
    const report = await runEvals({
      cases: dir,
      agents: [reads(2)],
      store,
      live: {
        judge: priced([verdictsReply([true])], "judge-model"),
        user: priced(
          userReplies(["And the repair?", { done: true }]),
          "user-model",
        ),
        budget: BUDGET,
      },
    });
    const id = report.cases[0]?.simulation?.user_thread_id ?? "";
    const evals = tenantStore(store, "evals");
    const opened = await openThread(evals, ThreadId.parse(id));
    if (!opened.ok) throw new Error("the user thread is kept");
    const timeline = await opened.value.timeline();
    if (!timeline.ok) throw new Error("the user thread reads");
    const asked = timeline.value.entries
      .map((e) => e.event)
      .filter((e) => e.type === "user_input")
      .map((e) => (e.type === "user_input" ? e.data.text : ""));
    expect(asked[0]).toBe(
      simulatedUserInput([
        { from: "user", text: "Please refund order 42." },
        { from: "agent", text: "Looked it up." },
      ]),
    );
    expect(asked[1]).toBe(
      simulatedUserInput([{ from: "agent", text: "Looked it up." }]),
    );
  });
});

describe("what ends the conversation", () => {
  test("a user that never finishes stops at max_messages, after that many agent runs", async () => {
    const dir = casesDir();
    await simulated(dir, "endless", model(3));
    const report = await runEvals({
      cases: dir,
      agents: [reads(3)],
      live: {
        judge: priced([verdictsReply([true])], "judge-model"),
        user: priced(
          userReplies(["Still no.", "Really?", "Come on."]),
          "user-model",
        ),
        budget: BUDGET,
      },
    });
    expect(report.cases[0]?.simulation?.ended).toBe("max_messages");
    expect(report.cases[0]?.simulation?.messages).toBe(3);
    // Three user messages, each answered by one agent run of two requests.
    expect(report.model_calls.agent).toBe(6);
    expect(report.cases[0]?.status).toBe("passed");
  });

  test("a script user needs no user model and ends with script_done", async () => {
    const dir = casesDir();
    await simulated(dir, "scripted", {
      kind: "script",
      messages: ["It's order 1234.", "Then the repair."],
    });
    const report = await runEvals({
      cases: dir,
      agents: [reads(3)],
      live: {
        judge: priced([verdictsReply([true])], "judge-model"),
        budget: BUDGET,
      },
    });
    expect(report.cases[0]?.simulation).toMatchObject({
      messages: 3,
      ended: "script_done",
      prefix: "none",
    });
    expect(report.model_calls.user).toBe(0);
    expect(report.cases[0]?.status).toBe("passed");
  });
});

describe("failures are values", () => {
  test("output the runner can't accept is simulator_invalid", async () => {
    const dir = casesDir();
    await simulated(dir, "bad-user", model());
    const report = await runEvals({
      cases: dir,
      agents: [reads(2)],
      live: {
        judge: priced([verdictsReply([true])], "judge-model"),
        // Output the schema rejects, twice: invalid after the one retry.
        user: priced(
          userReplies([
            { raw: { message: "Hello." } },
            { raw: { message: "Again." } },
          ]),
          "user-model",
        ),
        budget: BUDGET,
      },
    });
    expect([report.cases[0]?.status, report.cases[0]?.reason]).toEqual([
      "error",
      "simulator_invalid",
    ]);
  });

  test("an agent run that parks is an error; nobody answers approvals in an eval", async () => {
    const dir = casesDir();
    await saveTurns(
      dir,
      "parks",
      support(REFUND_TURN),
      ["Please refund order 42."],
      {
        rubric: RUBRIC,
        simulate: { kind: "script", messages: ["And order 43?"] },
        must: { type: "tool_call", data: { name: "issue_refund" } },
      },
    );
    // The agent now asks before refunding, and an eval answers no approval.
    const asks = agent({
      name: "support",
      instructions: "You handle refunds.",
      model: scriptedModel({ responses: REFUND_TURN }),
      tools: [lookupOrder, issueRefund],
      permissions: { allow: ["lookup_order"], ask: ["issue_refund"] },
    });
    const report = await runEvals({
      cases: dir,
      agents: [asks],
      live: {
        judge: priced([verdictsReply([true])], "judge-model"),
        budget: BUDGET,
      },
    });
    expect([report.cases[0]?.status, report.cases[0]?.reason]).toEqual([
      "error",
      "parked",
    ]);
  });

  test("a content-part opener skips the case", async () => {
    const dir = casesDir();
    await simulated(dir, "text-only", model());
    const path = join(dir, "text-only", "case.json");
    const meta = JSON.parse(readFileSync(path, "utf8"));
    meta.input = {};
    await Bun.write(path, `${JSON.stringify(meta, null, 2)}\n`);
    const report = await runEvals({
      cases: dir,
      agents: [reads(1)],
      live: {
        judge: priced([verdictsReply([true])], "judge-model"),
        user: priced(userReplies([{ done: true }]), "user-model"),
        budget: BUDGET,
      },
    });
    expect([report.cases[0]?.status, report.cases[0]?.reason]).toEqual([
      "skipped",
      "simulate_content_input",
    ]);
  });

  test("an unsettled prefix effect skips the simulation in live mode", async () => {
    const dir = casesDir();
    await simulated(dir, "open-effect", model());
    const path = join(dir, "open-effect", "case.json");
    const meta = JSON.parse(readFileSync(path, "utf8"));
    meta.simulate_blocked = "unsettled_effect";
    await Bun.write(path, `${JSON.stringify(meta, null, 2)}\n`);
    const report = await runEvals({
      cases: dir,
      agents: [reads(1)],
      live: {
        judge: priced([verdictsReply([true])], "judge-model"),
        user: priced(userReplies([{ done: true }]), "user-model"),
        budget: BUDGET,
      },
    });
    expect([report.cases[0]?.status, report.cases[0]?.reason]).toEqual([
      "skipped",
      "unsettled_effect",
    ]);
  });
});

describe("effects fail closed on every turn", () => {
  test("an effect with unrecorded arguments ends the case and issues no refund", async () => {
    const dir = casesDir();
    // The saved turn refunds order 42; the second simulated turn tries order 99.
    await saveTurns(
      dir,
      "new-effect",
      support(REFUND_TURN),
      ["Please refund order 42."],
      {
        rubric: RUBRIC,
        simulate: { kind: "script", messages: ["Now refund order 99."] },
        must: { type: "tool_call", data: { name: "issue_refund" } },
      },
    );
    // Recording the case issued a real refund; the eval must not add another.
    refunds.count = 0;
    const now = support([
      ...REFUND_TURN,
      use("issue_refund", { id: "99" }, "c3"),
      say("Refunded 99 too."),
    ]);
    const report = await runEvals({
      cases: dir,
      agents: [now],
      live: {
        judge: priced([verdictsReply([true])], "judge-model"),
        budget: BUDGET,
      },
    });
    expect(report.cases[0]?.status).toBe("error");
    expect(report.cases[0]?.reason).toBe("unmatched_external_op: issue_refund");
    expect(refunds.count).toBe(0);
  });
});
