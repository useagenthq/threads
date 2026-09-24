import { describe, expect, test } from "bun:test";
import { z } from "zod";
import {
  type Agent,
  agent,
  type Model,
  openThread,
  scriptedModel,
  sqlite,
  type ThreadRef,
  tool,
} from "../../src";
import { openStore } from "../../src/agent/sqlite";
import type { KnownEvent, Principal } from "../../src/log";
import { markTestKit } from "../../src/model/guard";
import { knownEvents } from "../../src/reduce";
import { unwrap } from "../store/helpers";

// Legacy background subagents wake their lead (spec/schema/README.md, "Background wakes" and
// "Run completion"; Gate 1 §2.7.3): a late result recorded while no turn is open carries a
// woken in the same append, the woken turn belongs to the run that spawned the child, and run()
// returns the answer after the last wake.

const usage = { input_tokens: 10, output_tokens: 2 };
const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});
const scan = (id: string) => ({
  content: [
    {
      type: "tool_use",
      call_id: id,
      name: "spawn_agent",
      input: { agent: "scanner", prompt: "Scan.", background: true },
    },
  ],
  stop_reason: "tool_use",
  usage,
});
const alice: Principal = { issuer: "api", tenant: "local", subject: "alice" };

/** A scripted model whose every answer waits for `open`. */
function gated(responses: readonly unknown[], open: Promise<void>): Model {
  const model = scriptedModel({ responses: [...responses] });
  const made: Model = {
    ...model,
    send: async function* (...args: Parameters<Model["send"]>) {
      await open;
      yield* model.send(...args);
    },
  };
  markTestKit(made);
  return made;
}

function lead(scanner: Agent<never, unknown>, responses: readonly unknown[]) {
  return agent({
    name: "lead",
    model: scriptedModel({ responses: [...responses] }),
    subagents: [scanner],
  });
}

async function events(
  store: ReturnType<typeof sqlite>,
  thread: ThreadRef,
): Promise<readonly KnownEvent[]> {
  const { log } = await openStore(store);
  return knownEvents(unwrap(log.read(thread.branch)));
}

/** Every late result is followed, after its append's block, by a woken naming the block. */
function wakes(log: readonly KnownEvent[]): readonly (readonly string[])[] {
  return log.flatMap((e) => (e.type === "woken" ? [e.data.causes] : []));
}

function lateIds(log: readonly KnownEvent[]): readonly string[] {
  return log.flatMap((e) =>
    e.type === "tool_result_late" ? [e.event_id] : [],
  );
}

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
              ...scan("c1").content,
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
  test("a late result after the lead was cancelled is recorded with no woken", async () => {
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
    expect(lateIds(log)).toHaveLength(1);
    expect(wakes(log)).toEqual([]);
  });
});
