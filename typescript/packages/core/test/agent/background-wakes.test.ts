import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { agent, openThread, scriptedModel, sqlite, tool } from "../../src";
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

// Legacy background subagents wake their lead (spec/schema/README.md, "Background wakes" and
// "Run completion"; Gate 1 §2.7.3): a late result recorded while no turn is open carries a
// woken in the same append, the woken turn belongs to the run that spawned the child, and run()
// returns the answer after the last wake.

describe("a background result wakes the idle lead", () => {
  test("the late result and its woken are one block, and run() returns the wake turn's answer", async () => {
    const store = sqlite(":memory:");
    const { promise, resolve } = Promise.withResolvers<void>();
    const scanner = agent({
      name: "scanner",
      model: gated([say("No vulnerable deps.")], promise),
    });
    const running = lead(scanner, [
      scan("c1"),
      say("Scan started."),
      say("The scan found nothing."),
    ]).stream("Scan in the background.", { store, principal: alice });
    for await (const item of running)
      if (item.kind === "event" && item.event.type === "turn_completed")
        resolve();
    const result = await running.result;
    expect(result).toMatchObject({
      status: "completed",
      output: "The scan found nothing.",
    });
    const log = await events(store, result.thread);
    const types = log.map((e) => e.type);
    const at = types.indexOf("agent_finished");
    expect(types.slice(at)).toEqual([
      "agent_finished",
      "tool_result_late",
      "woken",
      "model_request",
      "model_response",
      "turn_completed",
    ]);
    expect(wakes(log)).toEqual([lateIds(log)]);
    // The call is answered at once with a deferred placeholder; the child's result comes late.
    const results = log.flatMap((e) =>
      e.type === "tool_result" || e.type === "tool_result_late" ? [e] : [],
    );
    expect(results.map((e) => [e.type, e.data.preview])).toEqual([
      ["tool_result", "scanner started in the background"],
      ["tool_result_late", "No vulnerable deps."],
    ]);
    const woken = log.find((e) => e.type === "woken");
    expect(woken?.actor).toEqual({ kind: "host", principal: alice });
    // The first answer stays in the timeline.
    expect(
      log.filter((e) => e.type === "turn_completed").map((e) => e.seq),
    ).toHaveLength(2);
  });

  test("two children of one run ending at one boundary share one woken", async () => {
    const store = sqlite(":memory:");
    const { promise, resolve } = Promise.withResolvers<void>();
    const scanner = agent({
      name: "scanner",
      model: gated([say("Clean.")], promise),
    });
    const other = agent({
      name: "licenses",
      model: gated([say("MIT only.")], promise),
    });
    const both = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          {
            content: [
              {
                type: "tool_use",
                call_id: "c1",
                name: "spawn_agent",
                input: { agent: "scanner", prompt: "Scan.", background: true },
              },
              {
                type: "tool_use",
                call_id: "c2",
                name: "spawn_agent",
                input: {
                  agent: "licenses",
                  prompt: "Check.",
                  background: true,
                },
              },
            ],
            stop_reason: "tool_use",
            usage,
          },
          say("Both started."),
          say("Clean, and MIT only."),
        ],
      }),
      subagents: [scanner, other],
    });
    const running = both.stream("Scan and check.", { store, principal: alice });
    for await (const item of running)
      if (item.kind === "event" && item.event.type === "turn_completed")
        resolve();
    const result = await running.result;
    expect(result).toMatchObject({
      status: "completed",
      output: "Clean, and MIT only.",
    });
    const log = await events(store, result.thread);
    expect(wakes(log)).toEqual([lateIds(log)]);
    expect(lateIds(log)).toHaveLength(2);
  });
});

describe("no wake after a cancel", () => {
  test("a run cancelled mid-turn returns cancelled at once and is never woken", async () => {
    const store = sqlite(":memory:");
    const child = Promise.withResolvers<void>();
    const slow = Promise.withResolvers<void>();
    const scanner = agent({
      name: "scanner",
      model: gated([say("Clean.")], child.promise),
    });
    const wait = tool({
      name: "wait",
      description: "Wait.",
      input: z.object({}),
      runs: "host",
      effect: "read_only",
      execute: async () => {
        await slow.promise;
        return "waited";
      },
    });
    const bot = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          scan("c1"),
          {
            content: [
              { type: "tool_use", call_id: "c2", name: "wait", input: {} },
            ],
            stop_reason: "tool_use",
            usage,
          },
        ],
      }),
      subagents: [scanner],
      tools: [wait],
    });
    const running = bot.stream("Scan, then wait.", { store, principal: alice });
    for await (const item of running) {
      if (item.kind !== "event") continue;
      const { event } = item;
      if (event.type === "tool_call" && event.data.name === "wait") {
        const thread = unwrap(await openThread(store, event.thread_id));
        unwrap(await thread.cancel(alice));
        slow.resolve();
      }
      if (event.type === "turn_completed") child.resolve();
    }
    const result = await running.result;
    expect(result.status).toBe("cancelled");
    const log = await events(store, result.thread);
    expect(wakes(log)).toEqual([]);
  });
});
