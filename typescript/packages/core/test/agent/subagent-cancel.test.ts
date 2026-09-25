import { describe, expect, test } from "bun:test";
import { z } from "zod";
import {
  agent,
  extension,
  openThread,
  scriptedModel,
  sqlite,
  type ThreadRef,
  tool,
} from "../../src";
import { hostRunner } from "../../src/agent/registry";
import { openStore, storeConnection } from "../../src/agent/sqlite";
import { type KnownEvent, ThreadId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { unwrap } from "../store/helpers";
import { query } from "../team/kit";

// Tree-wide cancellation and parked children (spec/schema/README.md, "Subagent cancellation and // parking"): a cancelled parent cancels its running descendants and records each
// one's end before its own cancelled; a child that parks parks its parent, which resumes once
// the child's park is resolved.

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
const spawn = use("spawn_agent", { agent: "worker", prompt: "Do it." }, "s1");
const operator = { issuer: "api", tenant: "local", subject: "operator" };

type Store = ReturnType<typeof sqlite>;

async function events(
  store: Store,
  thread: ThreadRef | ThreadId,
): Promise<readonly KnownEvent[]> {
  const { log } = await openStore(store);
  const branch =
    typeof thread === "string"
      ? unwrap(await log.mainBranch(thread))
      : thread.branch;
  return knownEvents(unwrap(await log.read(branch)));
}

function childOf(parent: readonly KnownEvent[]): ThreadId {
  const spawned = parent.find((e) => e.type === "agent_spawned");
  if (spawned?.type !== "agent_spawned") throw new Error("no child");
  return spawned.data.child_thread_id;
}

function mailer(sent: string[]) {
  return tool({
    name: "send_email",
    description: "Send an email.",
    input: z.object({ to: z.string() }),
    runs: "host",
    execute: async ({ to }) => {
      sent.push(to);
      return "sent";
    },
  });
}

/** The parent's thread id, once both it and its child exist (the child is running). */
async function parentOnceChildRuns(store: Store): Promise<ThreadId> {
  for (let i = 0; i < 200; i++) {
    await Bun.sleep(5);
    const { db } = await storeConnection(store);
    const rows = z
      .array(z.object({ thread_id: ThreadId }))
      .parse(await query(db, "SELECT DISTINCT thread_id FROM branches", []));
    for (const { thread_id } of rows.length === 2 ? rows : []) {
      const log = await events(store, thread_id);
      if (log.some((e) => e.type === "agent_spawned")) return thread_id;
    }
  }
  throw new Error("the child never started");
}

describe("a child that parks parks its parent", () => {
  test("the parent ends parked on the child, then resumes and finishes once the child's approval is granted", async () => {
    const store = sqlite(":memory:");
    const sent: string[] = [];
    const mail = mailer(sent);
    const worker = agent({
      name: "worker",
      model: scriptedModel({
        responses: [use("send_email", { to: "bob" }, "m1"), say("Mailed.")],
      }),
      tools: [mail],
    });
    const lead = agent({
      name: "lead",
      model: scriptedModel({ responses: [spawn, say("All done.")] }),
      tools: [mail],
      subagents: [worker],
    });
    const result = await lead.run("Mail bob through the worker.", { store });
    const parent = await events(store, result.thread);
    const child = childOf(parent);
    expect(result).toMatchObject({
      status: "parked",
      reason: "awaiting_approval",
      pending: [{ kind: "child", id: child }],
    });
    expect(parent.some((e) => e.type === "agent_finished")).toBe(false);

    const handle = unwrap(await openThread(store, child));
    const [pending] = unwrap(await handle.pendingApprovals());
    if (pending === undefined) throw new Error("the child waits on approval");
    unwrap(await handle.approve(pending.challenge_id, operator));

    const runner = hostRunner(lead);
    if (runner === undefined) throw new Error("agent() registers a runner");
    const resumed = await runner.execute(
      { store, principal: operator, thread: result.thread },
      [],
    );
    expect(resumed).toMatchObject({ status: "completed", output: "All done." });
    expect(sent).toEqual(["bob"]);
    const after = await events(store, result.thread);
    const back = after.find((e) => e.type === "resumed");
    const spawned = after.find((e) => e.type === "agent_spawned");
    expect<unknown>(back?.type === "resumed" && back.data).toEqual({
      address: { kind: "child", id: child },
      cause_event_id: spawned?.event_id ?? "",
    });
    const finished = after.filter((e) => e.type === "agent_finished");
    expect(
      finished.map((e) => e.type === "agent_finished" && e.data.status),
    ).toEqual(["completed"]);
  });
});

describe("tree-wide cancellation (F7.6)", () => {
  test("cancelling a parent cancels its running child; the child's end is recorded before cancelled", async () => {
    const store = sqlite(":memory:");
    const { promise: gate, resolve: open } = Promise.withResolvers<void>();
    const slow = tool({
      name: "slow",
      description: "Waits.",
      input: z.object({}),
      runs: "host",
      effect: "read_only",
      execute: async () => {
        await gate;
        return "ok";
      },
    });
    const workerModel = scriptedModel({
      responses: [use("slow", {}, "w1"), use("slow", {}, "w2"), say("x")],
    });
    const worker = agent({
      name: "worker",
      model: workerModel,
      tools: [slow],
    });
    const lead = agent({
      name: "lead",
      model: scriptedModel({ responses: [spawn, say("never")] }),
      tools: [slow],
      subagents: [worker],
    });
    const running = lead.run("Go.", { store });
    const parentId = await parentOnceChildRuns(store);
    const parentThread = unwrap(await openThread(store, parentId));
    unwrap(await parentThread.cancel(operator));
    open();
    expect((await running).status).toBe("cancelled");

    const parent = await events(store, parentId);
    expect(
      parent
        .slice(parent.findIndex((e) => e.type === "cancel_requested"))
        .map((e) => e.type),
    ).toEqual([
      "cancel_requested",
      "agent_finished",
      "tool_result",
      "cancelled",
      "turn_completed",
    ]);
    const finished = parent.find((e) => e.type === "agent_finished");
    expect(finished?.type === "agent_finished" && finished.data.status).toBe(
      "cancelled",
    );
    const child = await events(store, childOf(parent));
    const barrier = child.findIndex((e) => e.type === "cancel_requested");
    expect(child[barrier]).toMatchObject({
      actor: { kind: "host", principal: operator },
      data: { scope: "tree" },
    });
    // Nothing new after the barrier: no model request, no second call.
    const later = child.slice(barrier).map((e) => e.type);
    expect(later).not.toContain("model_request");
    expect(later).toContain("cancelled");
    expect(workerModel.remaining()).toBe(2);
  });

  test("a subagent_stop continue after the barrier never runs the cancelled child again", async () => {
    const store = sqlite(":memory:");
    const { promise: gate, resolve: open } = Promise.withResolvers<void>();
    const slow = tool({
      name: "slow",
      description: "Waits.",
      input: z.object({}),
      runs: "host",
      effect: "read_only",
      execute: async () => {
        await gate;
        return "ok";
      },
    });
    const workerModel = scriptedModel({
      responses: [use("slow", {}, "w1"), say("after 1"), say("after 2")],
    });
    const worker = agent({ name: "worker", model: workerModel, tools: [slow] });
    const lead = agent({
      name: "lead",
      model: scriptedModel({ responses: [spawn, say("never")] }),
      tools: [slow],
      subagents: [worker],
      extensions: [
        extension({
          name: "keepgoing",
          hooks: {
            subagentStop: async () => ({
              decision: "continue",
              reason: "Keep going.",
            }),
          },
        }),
      ],
    });
    const running = lead.run("Go.", { store });
    const parentId = await parentOnceChildRuns(store);
    unwrap(await unwrap(await openThread(store, parentId)).cancel(operator));
    open();
    expect((await running).status).toBe("cancelled");
    const parent = await events(store, parentId);
    const child = await events(store, childOf(parent));
    const later = child
      .slice(child.findIndex((e) => e.type === "cancel_requested"))
      .map((e) => e.type);
    expect(later).not.toContain("model_request");
    expect(later).not.toContain("user_input");
    expect(workerModel.remaining()).toBe(2);
    const finished = parent.find((e) => e.type === "agent_finished");
    expect(finished?.type === "agent_finished" && finished.data.status).toBe(
      "cancelled",
    );
    // The hook's continue is recorded as the stop it amounts to, so no replay re-sends it.
    expect(
      parent.flatMap((e) =>
        e.type === "hook_decision" && e.data.hook === "subagent_stop"
          ? [e.data.decision]
          : [],
      ),
    ).toEqual(["stop"]);
  });
});
