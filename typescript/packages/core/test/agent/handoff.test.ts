import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { agent, scriptedModel, sqlite, type ThreadRef, tool } from "../../src";
import { openStore } from "../../src/agent/sqlite";
import type { KnownEvent } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { unwrap } from "../store/helpers";

// Handoffs (F7.10): the conversation moves to a new thread of a listed agent,
// which keeps the originating principal, pins its own line 0 and policy, and gets the forwarded
// history as untrusted reference. The old thread takes no input afterwards.

const usage = { input_tokens: 10, output_tokens: 2 };
const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});
const handoff = (agentName: string, id = "c1") => ({
  content: [
    {
      type: "tool_use",
      call_id: id,
      name: "handoff",
      input: { agent: agentName },
    },
  ],
  stop_reason: "tool_use",
  usage,
});
const alice = { issuer: "slack:T1", tenant: "acme", subject: "U42" };

async function events(
  store: ReturnType<typeof sqlite>,
  thread: ThreadRef,
): Promise<readonly KnownEvent[]> {
  const { log } = await openStore(store);
  return knownEvents(unwrap(log.read(thread.branch)));
}

describe("handoff", () => {
  test("records handoff and ends the turn; the target answers the pending request", async () => {
    const store = sqlite(":memory:");
    const billingModel = scriptedModel({ responses: [say("Refund issued.")] });
    const billing = agent({ name: "billing", model: billingModel });
    const front = agent({
      name: "front",
      model: scriptedModel({ responses: [handoff("billing")] }),
      handoffs: [billing],
    });
    const result = await front.run("I was double charged.", {
      store,
      principal: alice,
    });
    if (result.status !== "handed_off") throw new Error(result.status);
    const source = await events(store, result.thread);
    expect(source.slice(-3).map((e) => e.type)).toEqual([
      "handoff",
      "tool_result",
      "turn_completed",
    ]);
    const started = source.find((e) => e.type === "thread_started");
    expect(
      started?.type === "thread_started" && started.data.policy?.handoffs,
    ).toEqual(["billing"]);
    const moved = source.find((e) => e.type === "handoff");
    if (moved?.type !== "handoff") throw new Error("no handoff");
    expect(moved.data).toMatchObject({
      call_id: "c1",
      to_agent: "billing",
      forwarded: "transcript",
    });
    expect(result.to_thread.id).toBe(moved.data.to_thread_id);

    const target = await events(store, result.to_thread);
    const head = target.slice(0, 3);
    expect(head.map((e) => e.type)).toEqual([
      "thread_started",
      "injected",
      "user_input",
    ]);
    const [first, forwarded, input] = head;
    expect<unknown>(
      first?.type === "thread_started" && first.data.parent,
    ).toEqual({
      thread_id: result.thread.id,
      branch_id: result.thread.branch,
      event_id: moved.event_id,
      relation: "handoff",
    });
    expect(forwarded?.type === "injected" && forwarded.data).toMatchObject({
      source: "handoff",
      trust: "untrusted_reference",
      ref: moved.data.forwarded_ref ?? {},
    });
    expect(input?.type === "user_input" && [input.data, input.actor]).toEqual([
      { source: "handoff", text: "I was double charged." },
      { kind: "user", principal: alice },
    ]);
    expect(billingModel.remaining()).toBe(0);
  });

  test("a target the source didn't list fails before any effect; the turn goes on", async () => {
    const store = sqlite(":memory:");
    const billing = agent({
      name: "billing",
      model: scriptedModel({ responses: [] }),
    });
    const front = agent({
      model: scriptedModel({ responses: [handoff("legal"), say("I can't.")] }),
      handoffs: [billing],
    });
    const result = await front.run("Sue them.", { store });
    expect(result).toMatchObject({ status: "completed", output: "I can't." });
    const log = await events(store, result.thread);
    expect(log.some((e) => e.type === "handoff")).toBe(false);
    const res = log.find((e) => e.type === "tool_result");
    expect(res?.type === "tool_result" && res.data).toMatchObject({
      origin: "not_executed",
      preview: "not a handoff target: legal",
    });
  });

  test("the old thread takes no input: running it again reports the same target, once", async () => {
    const store = sqlite(":memory:");
    const billingModel = scriptedModel({ responses: [say("Refund issued.")] });
    const billing = agent({ name: "billing", model: billingModel });
    const front = agent({
      name: "front",
      model: scriptedModel({ responses: [handoff("billing")] }),
      handoffs: [billing],
    });
    const first = await front.run("I was double charged.", { store });
    if (first.status !== "handed_off") throw new Error(first.status);
    const again = await front.run("Hello?", { store, thread: first.thread });
    if (again.status !== "handed_off") throw new Error(again.status);
    expect(again.to_thread.id).toBe(first.to_thread.id);
    const source = await events(store, first.thread);
    expect(source.filter((e) => e.type === "user_input")).toHaveLength(1);
    const target = await events(store, first.to_thread);
    expect(target.filter((e) => e.type === "user_input")).toHaveLength(1);
    expect(billingModel.unexpected()).toBe(0);
  });
});

describe("the principal and host ceiling", () => {
  async function refund(ceiling?: { deny: string[] }) {
    const store = sqlite(":memory:");
    const refunded: string[] = [];
    const pay = tool({
      name: "refund",
      description: "Refund a charge.",
      input: z.object({ id: z.string() }),
      runs: "host",
      execute: async ({ id }) => {
        refunded.push(id);
        return "refunded";
      },
    });
    const billing = agent({
      name: "billing",
      model: scriptedModel({
        responses: [
          {
            content: [
              {
                type: "tool_use",
                call_id: "r1",
                name: "refund",
                input: { id: "ch_1" },
              },
            ],
            stop_reason: "tool_use",
            usage,
          },
          say("Done."),
        ],
      }),
      tools: [pay],
      permissions: { mode: "bypass", allow_bypass: true, allow: ["refund"] },
    });
    const front = agent({
      name: "front",
      model: scriptedModel({ responses: [handoff("billing")] }),
      handoffs: [billing],
    });
    const result = await front.run("Refund me.", {
      store,
      ...(ceiling === undefined ? {} : { ceiling }),
    });
    if (result.status !== "handed_off") throw new Error(result.status);
    const target = await events(store, result.to_thread);
    const decision = target.find((e) => e.type === "permission_decision");
    return { refunded, decision };
  }

  test("a handoff target's own allow runs without a ceiling", async () => {
    const { refunded, decision } = await refund();
    expect(refunded).toEqual(["ch_1"]);
    expect(
      decision?.type === "permission_decision" && decision.data,
    ).toMatchObject({
      decision: "allow",
    });
  });

  test("the run's ceiling caps the target: its deny wins over the target's allow", async () => {
    const { refunded, decision } = await refund({ deny: ["refund"] });
    expect(refunded).toEqual([]);
    expect(
      decision?.type === "permission_decision" && decision.data,
    ).toMatchObject({
      decision: "deny",
      source: "policy",
      rule_id: "refund",
    });
  });
});

describe("a subagent's handoff (spec/schema/README.md, Handoff scope)", () => {
  const use = (name: string, input: Record<string, unknown>, id: string) => ({
    content: [{ type: "tool_use", call_id: id, name, input }],
    stop_reason: "tool_use",
    usage,
  });

  function tree(refunded: string[], rootDeny?: readonly string[]) {
    const refund = tool({
      name: "refund",
      description: "Refund a charge.",
      input: z.object({ id: z.string() }),
      runs: "host",
      execute: async ({ id }) => {
        refunded.push(id);
        return "refunded";
      },
    });
    const billing = agent({
      name: "billing",
      model: scriptedModel({
        responses: [use("refund", { id: "ch_1" }, "r1"), say("Done.")],
      }),
      tools: [refund],
      permissions: { mode: "bypass", allow_bypass: true, allow: ["refund"] },
    });
    const mid = agent({
      name: "mid",
      model: scriptedModel({ responses: [handoff("billing", "h1")] }),
      handoffs: [billing],
    });
    const unused = agent({
      name: "unused",
      model: scriptedModel({ responses: [] }),
    });
    return agent({
      name: "front",
      model: scriptedModel({
        responses: [
          use("spawn_agent", { agent: "mid", prompt: "Refund me." }, "s1"),
          say("ok"),
        ],
      }),
      subagents: [mid],
      // handoff is among front's tools, so it survives the child's tool filter.
      handoffs: [unused],
      ...(rootDeny === undefined
        ? {}
        : { permissions: { deny: [...rootDeny] } }),
    });
  }

  test("the run's ceiling caps the target of a subagent's handoff", async () => {
    const refunded: string[] = [];
    await tree(refunded).run("Refund me.", {
      store: sqlite(":memory:"),
      ceiling: { deny: ["refund"] },
    });
    expect(refunded).toEqual([]);
  });

  test("an ancestor's policy caps the target of a subagent's handoff", async () => {
    const refunded: string[] = [];
    await tree(refunded, ["refund"]).run("Refund me.", {
      store: sqlite(":memory:"),
    });
    expect(refunded).toEqual([]);
  });
});
