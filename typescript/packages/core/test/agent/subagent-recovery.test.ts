import { describe, expect, test } from "bun:test";
import {
  type Agent,
  agent,
  extension,
  scriptedModel,
  sqlite,
  type ThreadRef,
} from "../../src";
import {
  childFactory,
  enforcement,
  register,
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
  const parent = knownEvents(unwrap(log.read(thread.branch)));
  const spawned = parent.filter((e) => e.type === "agent_spawned");
  const first = spawned[0];
  if (spawned.length !== 1 || first?.type !== "agent_spawned")
    throw new Error(`expected one agent_spawned, got ${spawned.length}`);
  const branch = unwrap(log.mainBranch(first.data.child_thread_id));
  return { parent, child: knownEvents(unwrap(log.read(branch))) };
}

const count = (log: readonly KnownEvent[], type: KnownEvent["type"]) =>
  log.filter((e) => e.type === type).length;

describe("recovery after a crash mid-subagent", () => {
  test("crash after agent_spawned, before the child started: the same child runs once", async () => {
    const store = sqlite(":memory:");
    const setup = (fail: boolean) =>
      extension({
        name: "boot",
        setup: async () => {
          if (fail) throw new Error("process killed");
        },
      });
    const lead = (child: Agent<never, unknown>, responses: unknown[]) =>
      agent({
        name: "lead",
        model: scriptedModel({ responses }),
        subagents: [child],
      });
    const crashing = agent({
      name: "reviewer",
      model: scriptedModel({ responses: [] }),
      extensions: [setup(true)],
    });
    const { thread } = await lead(crashing, [say("Hi.")]).run("Hi", { store });
    const first = lead(crashing, [spawn]).run("Review the diff.", {
      store,
      thread,
    });
    expect(first).rejects.toThrow("process killed");
    await first.catch(() => undefined);

    const model = scriptedModel({ responses: [say("LGTM")] });
    const reviewer = agent({
      name: "reviewer",
      model,
      extensions: [setup(false)],
    });
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
    if (real === undefined || target === undefined || enforce === undefined)
      throw new Error("agent() registers every handle");
    register(reviewer, {
      target,
      enforce,
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
    expect(first).rejects.toThrow("process killed");

    register(reviewer, { child: real, target, enforce });
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
