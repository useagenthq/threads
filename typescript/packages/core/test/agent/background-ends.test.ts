import { describe, expect, test } from "bun:test";
import {
  agent,
  type Model,
  openThread,
  scriptedModel,
  sqlite,
} from "../../src";
import { markTestKit } from "../../src/model/guard";
import { unwrap } from "../store/helpers";
import {
  alice,
  events,
  gated,
  lateIds,
  lead,
  say,
  scan,
  usage,
  wakes,
} from "./wake-kit";

// When a run's end is decided otherwise, it is not woken and run() returns at once
// (spec/schema/README.md, "Run completion" and "Background wakes"): a failed or budget-exhausted
// turn, a cancel while the lead waits, and a handoff. A wake turn can also spawn another child,
// and the run then waits for that one too.

const types = (log: readonly { readonly type: string }[]): string =>
  log.map((e) => e.type).join(" ");

describe("a run whose end is decided is not woken", () => {
  test("a failed first turn returns at once, with its child still running", async () => {
    const store = sqlite(":memory:");
    const gate = Promise.withResolvers<void>();
    const scanner = agent({
      name: "scanner",
      model: gated([say("Clean.")], gate.promise),
    });
    const result = await lead(scanner, [
      scan("c1"),
      { error: { reason: "prompt_too_long", http_status: 400 } },
      say("never"),
    ]).run("Scan.", { store, principal: alice });
    gate.resolve();
    expect(result.status).toBe("failed");
    expect(wakes(await events(store, result.thread))).toEqual([]);
  });

  test("a wake turn that exhausts the budget ends the run budget_exhausted", async () => {
    const store = sqlite(":memory:");
    const gate = Promise.withResolvers<void>();
    const scanner = agent({
      name: "scanner",
      model: gated([say("Clean.")], gate.promise),
    });
    const running = lead(scanner, [
      scan("c1"),
      say("Started."),
      say("x"),
    ]).stream("Scan.", {
      store,
      principal: alice,
      budget: { max_model_requests: 2 },
    });
    for await (const item of running)
      if (item.kind === "event" && item.event.type === "turn_completed")
        gate.resolve();
    const result = await running.result;
    expect(result.status).toBe("budget_exhausted");
    const log = await events(store, result.thread);
    expect(wakes(log)).toEqual([lateIds(log)]);
  });

  test("a cancel while the lead waits stops the run: no wake, cancelled", async () => {
    const store = sqlite(":memory:");
    const gate = Promise.withResolvers<void>();
    const scanner = agent({
      name: "scanner",
      model: gated([say("Clean.")], gate.promise),
    });
    const running = lead(scanner, [
      scan("c1"),
      say("Started."),
      say("never"),
    ]).stream("Scan.", { store, principal: alice });
    for await (const item of running)
      if (item.kind === "event" && item.event.type === "turn_completed") {
        const thread = unwrap(await openThread(store, item.event.thread_id));
        unwrap(await thread.cancel(alice));
        gate.resolve();
      }
    const result = await running.result;
    expect(result.status).toBe("cancelled");
    expect(wakes(await events(store, result.thread))).toEqual([]);
  });

  test("a lead that hands off with a child running is never woken", async () => {
    const store = sqlite(":memory:");
    const gate = Promise.withResolvers<void>();
    const scanner = agent({
      name: "scanner",
      model: gated([say("Clean.")], gate.promise),
    });
    const target = agent({
      name: "target",
      model: scriptedModel({ responses: [say("Target here.")] }),
    });
    const handoff = {
      content: [
        {
          type: "tool_use",
          call_id: "h1",
          name: "handoff",
          input: { agent: "target" },
        },
      ],
      stop_reason: "tool_use",
      usage,
    };
    const bot = agent({
      name: "lead",
      model: scriptedModel({ responses: [scan("c1"), handoff, say("never")] }),
      subagents: [scanner],
      handoffs: [target],
    });
    const result = await bot.run("Scan, then hand off.", {
      store,
      principal: alice,
    });
    gate.resolve();
    expect(result.status).toBe("handed_off");
    const source = result.status === "handed_off" ? result.thread : undefined;
    expect(source).toBeDefined();
  });
});

/** A scripted model whose n-th answer waits for the lead's n-th completed turn. */
function stepped(responses: readonly unknown[]): {
  readonly model: Model;
  readonly turnEnded: () => void;
} {
  const model = scriptedModel({ responses: [...responses] });
  const gates = responses.map(() => Promise.withResolvers<void>());
  let asked = 0;
  let ended = 0;
  const made: Model = {
    ...model,
    send: async function* (...args: Parameters<Model["send"]>) {
      await gates[asked++]?.promise;
      yield* model.send(...args);
    },
  };
  markTestKit(made);
  return { model: made, turnEnded: () => gates[ended++]?.resolve() };
}

describe("a nested background child", () => {
  test("a wake turn that spawns another child waits for it too", async () => {
    const store = sqlite(":memory:");
    const kid = stepped([say("A is clean."), say("B is clean.")]);
    const scanner = agent({ name: "scanner", model: kid.model });
    const running = lead(scanner, [
      scan("c1"),
      say("Started A."),
      scan("c2"),
      say("Started B."),
      say("Both are clean."),
    ]).stream("Scan twice.", { store, principal: alice });
    for await (const item of running)
      if (item.kind === "event" && item.event.type === "turn_completed")
        kid.turnEnded();
    const result = await running.result;
    expect(result).toMatchObject({
      status: "completed",
      output: "Both are clean.",
    });
    const log = await events(store, result.thread);
    expect(wakes(log)).toHaveLength(2);
    expect(types(log)).toContain("woken");
  });
});
