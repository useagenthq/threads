import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { agent, scriptedModel, sqlite, tool } from "../../src";
import { hostRunner } from "../../src/agent/registry";
import { openStore } from "../../src/agent/sqlite";
import type { BranchId, KnownEvent, ThreadId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { unwrap } from "../store/helpers";

// A child that can't run right now is not a child that ended: a busy lease records nothing (the
// parent halts, or stays parked on it), and a child whose start was torn by a crash starts on resume.

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
const operator = { issuer: "api", tenant: "local", subject: "operator" };

type Store = ReturnType<typeof sqlite>;

async function read(
  store: Store,
  branch: BranchId,
): Promise<readonly KnownEvent[]> {
  const { log } = await openStore(store);
  return knownEvents(unwrap(log.read(branch)));
}

describe("child runs that don't end", () => {
  test("a child whose lease another process holds is not recorded as finished", async () => {
    const store = sqlite(":memory:");
    const mail = tool({
      name: "send_email",
      description: "Send.",
      input: z.object({ to: z.string() }),
      runs: "host",
      execute: async () => "sent",
    });
    const worker = agent({
      name: "worker",
      model: scriptedModel({
        responses: [use("send_email", { to: "bob" }, "m1"), say("Mailed.")],
      }),
      tools: [mail],
    });
    const lead = agent({
      name: "lead",
      model: scriptedModel({
        responses: [
          use("spawn_agent", { agent: "worker", prompt: "Mail bob." }, "s1"),
          say("All done."),
        ],
      }),
      tools: [mail],
      subagents: [worker],
    });
    const first = await lead.run("go", { store });
    expect(first.status).toBe("parked");
    const spawned = (await read(store, first.thread.branch)).find(
      (e) => e.type === "agent_spawned",
    );
    if (spawned?.type !== "agent_spawned") throw new Error("no child");
    const { log } = await openStore(store);
    const child = unwrap(log.mainBranch(spawned.data.child_thread_id));
    unwrap(log.acquire(child, "zombie-process"));
    const runner = hostRunner(lead);
    if (runner === undefined) throw new Error("agent() registers a runner");
    const second = await runner.execute(
      { store, principal: operator, thread: first.thread },
      [],
    );
    // The parent stays parked on the child it couldn't run; nothing ends the child.
    expect(second).toMatchObject({ status: "parked" });
    const after = await read(store, first.thread.branch);
    expect(after.some((e) => e.type === "agent_finished")).toBe(false);
  });

  test("a child branch created without its thread_started starts on resume", async () => {
    const store = sqlite(":memory:");
    const { log } = await openStore(store);
    const child = agent({
      name: "child",
      model: scriptedModel({ responses: [say("child answer")] }),
    });
    const parent = agent({
      name: "parent",
      model: scriptedModel({
        responses: [
          use("spawn_agent", { agent: "child", prompt: "hi" }, "s1"),
          say("parent done"),
        ],
      }),
      subagents: [child],
    });
    const create = log.createBranch.bind(log);
    const threads: ThreadId[] = [];
    log.createBranch = (threadId, branchId) => {
      const made = create(threadId, branchId);
      threads.push(threadId);
      if (threads.length === 2) throw new Error("crash after createBranch");
      return made;
    };
    await expect(parent.run("go", { store })).rejects.toThrow("crash");
    log.createBranch = create;
    const [parentId, childId] = threads;
    if (parentId === undefined || childId === undefined)
      throw new Error("both threads were created");
    const runner = hostRunner(parent);
    if (runner === undefined) throw new Error("agent() registers a runner");
    const resumed = await runner.execute(
      {
        store,
        principal: operator,
        thread: {
          id: parentId,
          branch: unwrap(log.mainBranch(parentId)),
          store,
        },
      },
      [],
    );
    expect(resumed).toMatchObject({
      status: "completed",
      output: "parent done",
    });
    const childLog = await read(store, unwrap(log.mainBranch(childId)));
    expect(childLog[0]?.type).toBe("thread_started");
    const finished = (await read(store, unwrap(log.mainBranch(parentId)))).find(
      (e) => e.type === "agent_finished",
    );
    expect(finished?.type === "agent_finished" && finished.data.status).toBe(
      "completed",
    );
  });
});
