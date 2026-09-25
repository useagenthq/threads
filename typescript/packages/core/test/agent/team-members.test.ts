import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { agent, scriptedModel, sqlite, tool } from "../../src";
import { openStore } from "../../src/agent/sqlite";
import { knownEvents } from "../../src/reduce";
import { unwrap } from "../store/helpers";

// A member is its agent name (spec/schema/README.md, Team tools): a lead never has two
// unfinished children of one name, so two instances can't share a member's tasks and messages.

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

describe("one member per agent name", () => {
  test("spawning a name whose child is still running is refused; once it ends it can be spawned again", async () => {
    const store = sqlite(":memory:");
    const { promise: gate, resolve: open } = Promise.withResolvers<void>();
    const wait = tool({
      name: "wait",
      description: "Waits.",
      input: z.object({}),
      runs: "host",
      effect: "read_only",
      execute: async () => {
        await gate;
        return "ok";
      },
    });
    const worker = agent({
      name: "worker",
      model: scriptedModel({
        responses: [use("wait", {}, "w1"), say("one done"), say("two done")],
      }),
      tools: [wait],
    });
    const spawn = (id: string, background: boolean) =>
      use("spawn_agent", { agent: "worker", prompt: "Work.", background }, id);
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [spawn("s1", true), spawn("s2", false), say("waiting")],
      }),
      tools: [wait],
      subagents: [worker],
    });
    const running = lead.run("go", { store });
    // The second spawn is decided while the first child waits.
    await Bun.sleep(50);
    open();
    const first = await running;
    const { log } = await openStore(store);
    const events = knownEvents(unwrap(await log.read(first.thread.branch)));
    expect(
      events.filter((e) => e.type === "agent_spawned").map((e) => e.data),
    ).toMatchObject([{ call_id: "s1" }]);
    const refused = events.find(
      (e) => e.type === "tool_result" && e.data.call_id === "s2",
    );
    expect(refused?.type === "tool_result" && refused.data).toMatchObject({
      is_error: true,
      preview: "member_active: worker is still running",
    });

    const again = await agent({
      name: "lead",
      model: scriptedModel({ responses: [spawn("s3", false), say("ok")] }),
      tools: [wait],
      subagents: [worker],
    }).run("again", { store, thread: first.thread });
    expect(again.status).toBe("completed");
    const after = knownEvents(unwrap(await log.read(first.thread.branch)));
    expect(
      after.filter((e) => e.type === "agent_spawned").map((e) => e.data),
    ).toMatchObject([{ call_id: "s1" }, { call_id: "s3" }]);
  });
});
