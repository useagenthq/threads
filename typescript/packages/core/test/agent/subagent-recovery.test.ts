import { describe, expect, test } from "bun:test";
import {
  type Agent,
  agent,
  scriptedModel,
  sqlite,
  type ThreadRef,
} from "../../src";
import {
  childFactory,
  enforcement,
  memberEntry,
  register,
  setupOf,
  targetFactory,
} from "../../src/agent/registry";
import { openStore } from "../../src/agent/sqlite";
import type { KnownEvent } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { unwrap } from "../store/helpers";

// A crash mid-subagent (invariant 3): the restarted parent finds the child by
// the agent_spawned it recorded first, resumes that same child thread, never prompts it twice,
// and records exactly one agent_finished.

const usage = { input_tokens: 10, output_tokens: 2 };
const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});
const spawn = {
  content: [
    {
      type: "tool_use",
      call_id: "c1",
      name: "spawn_agent",
      input: { agent: "reviewer", prompt: "Review it." },
    },
  ],
  stop_reason: "tool_use",
  usage,
};

type Store = ReturnType<typeof sqlite>;

async function logs(
  store: Store,
  thread: ThreadRef,
): Promise<{
  readonly parent: readonly KnownEvent[];
  readonly child: readonly KnownEvent[];
}> {
  const { log } = await openStore(store);
  const parent = knownEvents(unwrap(await log.read(thread.branch)));
  const spawned = parent.filter((e) => e.type === "agent_spawned");
  const first = spawned[0];
  if (spawned.length !== 1 || first?.type !== "agent_spawned")
    throw new Error(`expected one agent_spawned, got ${spawned.length}`);
  const branch = unwrap(await log.mainBranch(first.data.child_thread_id));
  return { parent, child: knownEvents(unwrap(await log.read(branch))) };
}

const count = (log: readonly KnownEvent[], type: KnownEvent["type"]) =>
  log.filter((e) => e.type === type).length;

describe("recovery after a crash mid-subagent", () => {
  test("crash after agent_spawned, before the child started: the same child runs once", async () => {
    const store = sqlite(":memory:");
    const lead = (child: Agent<never, unknown>, responses: unknown[]) =>
      agent({
        name: "lead",
        model: scriptedModel({ responses }),
        subagents: [child],
      });
    // The process dies as the child is about to start: nothing of it is recorded. (A failing
    // setup can't stand in: a parent sets its subagents up before its own first run.)
    const crashing = agent({
      name: "reviewer",
      model: scriptedModel({ responses: [] }),
    });
    const real = childFactory(crashing);
    const target = targetFactory(crashing);
    const enforce = enforcement(crashing);
    const setup = setupOf(crashing);
    const member = memberEntry(crashing);
    if (
      real === undefined ||
      target === undefined ||
      enforce === undefined ||
      setup === undefined ||
      member === undefined
    )
      throw new Error("agent() registers every handle");
    register(crashing, {
      setup,
      target,
      enforce,
      member,
      child: (env) => ({
        ...real(env),
        run: async () => {
          throw new Error("process killed");
        },
      }),
    });
    const { thread } = await lead(crashing, [say("Hi.")]).run("Hi", { store });
    const first = lead(crashing, [spawn]).run("Review the diff.", {
      store,
      thread,
    });
    await expect(first).rejects.toThrow("process killed");
    await first.catch(() => undefined);

    const model = scriptedModel({ responses: [say("LGTM")] });
    const reviewer = agent({ name: "reviewer", model });
    const again = await lead(reviewer, [say("Reviewed."), say("Done.")]).run(
      "Anything else?",
      { store, thread },
    );
    expect(again).toMatchObject({ status: "completed", output: "Done." });
    const { parent, child } = await logs(store, again.thread);
    expect(count(parent, "agent_finished")).toBe(1);
    expect(count(child, "user_input")).toBe(1);
    expect({
      remaining: model.remaining(),
      unexpected: model.unexpected(),
    }).toEqual({
      remaining: 0,
      unexpected: 0,
    });
  });

  test("crash after the child finished, before agent_finished: its result is read, not re-run", async () => {
    const store = sqlite(":memory:");
    const model = scriptedModel({ responses: [say("LGTM")] });
    const reviewer = agent({ name: "reviewer", model });
    const real = childFactory(reviewer);
    const target = targetFactory(reviewer);
    const enforce = enforcement(reviewer);
    const setup = setupOf(reviewer);
    const member = memberEntry(reviewer);
    if (
      real === undefined ||
      target === undefined ||
      enforce === undefined ||
      setup === undefined ||
      member === undefined
    )
      throw new Error("agent() registers every handle");
    register(reviewer, {
      setup,
      target,
      enforce,
      member,
      child: (env) => {
        const sub = real(env);
        return {
          ...sub,
          run: async (child) => {
            await sub.run(child);
            throw new Error("process killed");
          },
        };
      },
    });
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [say("Hi."), spawn, say("Reviewed."), say("Done.")],
      }),
      subagents: [reviewer],
    });
    const { thread } = await lead.run("Hi", { store });
    const first = lead.run("Review the diff.", { store, thread });
    await first.catch(() => undefined);
    await expect(first).rejects.toThrow("process killed");

    register(reviewer, { setup, child: real, target, enforce, member });
    const again = await lead.run("Thanks.", { store, thread });
    expect(again.status).toBe("completed");
    const { parent, child } = await logs(store, again.thread);
    expect(count(parent, "agent_finished")).toBe(1);
    expect(count(child, "user_input")).toBe(1);
    expect(count(child, "model_request")).toBe(1);
    const result = parent.find((e) => e.type === "tool_result");
    expect(result?.type === "tool_result" && result.data.preview).toBe("LGTM");
  });
});
