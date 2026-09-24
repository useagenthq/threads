import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { agent, scriptedModel, sqlite, tool } from "../../src";
import { openStore } from "../../src/agent/sqlite";
import type { KnownEvent } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { unwrap } from "../store/helpers";
import { memberEvents, start } from "../team/run-kit";
import { eventsOf, type Store, say, startedOf, use } from "./tool-search-kit";

// Children inherit their parent's resolved defer_tools unless they set it (spec/schema/README.md,
// "Deferred tools and tool_search"): a subagent, a handoff target and a team member. Each gets
// its own tool_search and starts with nothing loaded.

const echo = tool({
  name: "echo",
  description: "Repeat the text.",
  input: z.object({ text: z.string() }),
  runs: "host",
  effect: "read_only",
  execute: async ({ text }) => text,
});

/** The child's pinned defer_tools, and its tools as [name, deferred]. */
function pinOf(events: readonly KnownEvent[]) {
  const started = startedOf(events);
  return {
    defer: started.data.policy?.context?.defer_tools,
    tools: started.data.tools.map((t) => [t.name, t.defer_loading === true]),
    loaded: events.some((e) => e.type === "tools_loaded"),
  };
}

async function childOf(store: Store, parent: readonly KnownEvent[]) {
  const spawned = parent.find((e) => e.type === "agent_spawned");
  if (spawned?.type !== "agent_spawned") throw new Error("no child");
  const { log } = await openStore(store);
  const branch = unwrap(log.mainBranch(spawned.data.child_thread_id));
  return knownEvents(unwrap(log.read(branch)));
}

const spawn = use(
  "spawn_agent",
  { agent: "reviewer", prompt: "Review." },
  "s1",
);

describe("a child's deferral", () => {
  test("a subagent that doesn't set defer_tools pins its parent's always; one that sets it keeps its own", async () => {
    for (const [own, want] of [
      [undefined, "always"],
      ["never", "never"],
    ] as const) {
      const store = sqlite(":memory:");
      const reviewer = agent({
        name: "reviewer",
        model: scriptedModel({ responses: [say("LGTM")] }),
        tools: [echo],
        ...(own === undefined ? {} : { context: { defer_tools: own } }),
      });
      const lead = agent({
        name: "lead",
        model: scriptedModel({
          responses: [
            use("tool_search", { query: "echo" }, "c0"),
            spawn,
            say("ok"),
          ],
        }),
        tools: [echo],
        subagents: [reviewer],
        context: { defer_tools: "always" },
      });
      const result = await lead.run("Go.", { store });
      const parent = await eventsOf(store, result.thread);
      // The parent loaded echo; the child starts with nothing loaded.
      expect(parent.some((e) => e.type === "tools_loaded")).toBe(true);
      const child = pinOf(await childOf(store, parent));
      expect(child.defer).toBe(want);
      expect(child.loaded).toBe(false);
      expect(child.tools).toContainEqual(["echo", want === "always"]);
      expect(child.tools.some(([n]) => n === "tool_search")).toBe(
        want === "always",
      );
    }
  });

  test("a handoff target inherits the handing-off thread's defer_tools", async () => {
    const store = sqlite(":memory:");
    const billing = agent({
      name: "billing",
      model: scriptedModel({ responses: [say("Refunded.")] }),
      tools: [echo],
    });
    const front = agent({
      name: "front",
      model: scriptedModel({
        responses: [use("handoff", { agent: "billing" }, "h1")],
      }),
      handoffs: [billing],
      context: { defer_tools: "always" },
    });
    const result = await front.run("Refund me.", { store });
    if (result.status !== "handed_off") throw new Error(result.status);
    const target = pinOf(await eventsOf(store, result.to_thread));
    expect(target.defer).toBe("always");
    expect(target.tools).toContainEqual(["echo", true]);
    expect(target.tools).toContainEqual(["tool_search", false]);
  });

  test("a team member inherits the lead's defer_tools (and its rebind agrees)", async () => {
    const store = sqlite(":memory:");
    const researcher = agent({
      name: "researcher",
      model: scriptedModel({ responses: [say("Done.")] }),
      tools: [echo],
    });
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          start("c1", "researcher", "Go."),
          say("Started."),
          say("All done."),
        ],
      }),
      team: [researcher],
      context: { defer_tools: "always" },
    });
    const r = await lead.run("Work.", { store });
    expect(r.status).toBe("completed");
    const member = pinOf(
      await memberEvents(store, r.team.ref.id, "researcher-1"),
    );
    expect(member.defer).toBe("always");
    expect(member.tools).toContainEqual(["echo", true]);
  });
});
