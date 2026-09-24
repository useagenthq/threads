import { describe, expect, test } from "bun:test";
import { agent, fakeSandbox, scriptedModel, sqlite } from "../../src";
import { unwrap } from "../store/helpers";

// RunResult.thread is the Thread handle (spec/api.json RunResult.thread: #/types/Thread), as in
// Python: a finished run's thread is read without openThread.

const usage = { input_tokens: 10, output_tokens: 2 };
const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});
const use = (name: string, input: Record<string, unknown>, id: string) => ({
  content: [{ type: "tool_use", call_id: id, name, input }],
  stop_reason: "tool_use",
  usage,
});

describe("result.thread is a Thread handle", () => {
  test("usage, cost, timeline and replay read the run's thread directly", async () => {
    const bot = agent({ model: scriptedModel({ responses: [say("Hello!")] }) });
    const result = await bot.run("hi", { store: sqlite(":memory:") });
    expect(result.status).toBe("completed");
    const totals = unwrap(await result.thread.usage());
    expect([totals.input_tokens, totals.output_tokens]).toEqual([10, 2]);
    expect(unwrap(await result.thread.cost())).toBeNull();
    const timeline = unwrap(await result.thread.timeline());
    expect(timeline.thread_id).toBe(result.thread.id);
    expect(timeline.entries.at(-1)?.event.type).toBe("turn_completed");
    expect((await result.thread.replay()).ok).toBe(true);
  });

  test("it continues the thread when passed back to run", async () => {
    const store = sqlite(":memory:");
    const bot = agent({
      model: scriptedModel({ responses: [say("one"), say("two")] }),
    });
    const first = await bot.run("a", { store });
    const second = await bot.run("b", { store, thread: first.thread });
    expect(second.thread.id).toBe(first.thread.id);
    const inputs = unwrap(await second.thread.timeline()).entries.filter(
      (e) => e.event.type === "user_input",
    );
    expect(inputs).toHaveLength(2);
  });

  test("a sandboxed run's thread forks with the agent's sandbox, and the child runs", async () => {
    const store = sqlite(":memory:");
    const bot = agent({
      model: scriptedModel({
        responses: [
          use("write", { path: "a.txt", content: "A" }, "c1"),
          say("done"),
          say("again"),
        ],
      }),
      sandbox: fakeSandbox(),
      permissions: { mode: "accept_edits" },
    });
    const first = await bot.run("write", { store });
    const [point] = await first.thread.forkPoints();
    if (point === undefined) throw new Error("no fork point");
    const child = unwrap(await first.thread.fork(point));
    expect(child.branch).not.toBe(first.thread.branch);
    const next = await bot.run("more", { store, thread: child });
    expect(next.status).toBe("completed");
    expect(next.thread.branch).toBe(child.branch);
  });

  test("a handed-off result's target thread is a handle too", async () => {
    const billing = agent({
      name: "billing",
      model: scriptedModel({ responses: [say("Refund issued.")] }),
    });
    const front = agent({
      name: "front",
      model: scriptedModel({
        responses: [use("handoff", { agent: "billing" }, "h1")],
      }),
      handoffs: [billing],
    });
    const result = await front.run("I was double charged.", {
      store: sqlite(":memory:"),
    });
    if (result.status !== "handed_off") throw new Error(result.status);
    const target = unwrap(await result.to_thread.timeline());
    expect(target.thread_id).toBe(result.to_thread.id);
    expect(target.entries.some((e) => e.event.type === "turn_completed")).toBe(
      true,
    );
  });
});
