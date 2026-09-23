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
import { openStore } from "../../src/agent/sqlite";
import type { KnownEvent } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { unwrap } from "../store/helpers";

// Subagents are child threads (F7.1, F7.2, F7.5, F7.8): agent_spawned is
// durable before the child starts, the child only narrows its parent, and the parent records
// exactly one agent_finished per child.

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
const spawn = (input: Record<string, unknown>, id = "c1") =>
  use("spawn_agent", { agent: "reviewer", prompt: "Review it.", ...input }, id);

type Store = ReturnType<typeof sqlite>;

async function events(
  store: Store,
  thread: ThreadRef,
): Promise<readonly KnownEvent[]> {
  const { log } = await openStore(store);
  return knownEvents(unwrap(log.read(thread.branch)));
}

/** The log of the child the parent's first agent_spawned names. */
async function childEvents(
  store: Store,
  parent: readonly KnownEvent[],
): Promise<readonly KnownEvent[]> {
  const spawned = parent.find((e) => e.type === "agent_spawned");
  if (spawned?.type !== "agent_spawned") throw new Error("no child");
  const { log } = await openStore(store);
  const branch = unwrap(log.mainBranch(spawned.data.child_thread_id));
  return knownEvents(unwrap(log.read(branch)));
}

function only<T extends KnownEvent["type"]>(
  log: readonly KnownEvent[],
  type: T,
): Extract<KnownEvent, { type: T }>[] {
  return log.filter(
    (e): e is Extract<KnownEvent, { type: T }> => e.type === type,
  );
}

const echo = tool({
  name: "echo",
  description: "Repeat the text.",
  input: z.object({ text: z.string() }),
  runs: "host",
  effect: "read_only",
  execute: async ({ text }) => text,
});

describe("spawn_agent in the foreground", () => {
  test("the child runs in its own thread, linked to agent_spawned; its result is the call's", async () => {
    const store = sqlite(":memory:");
    const reviewer = agent({
      name: "reviewer",
      model: scriptedModel({ responses: [say("LGTM")] }),
      tools: [echo],
      budget: { max_model_requests: 5 },
    });
    const lead = agent({
      name: "lead",
      model: scriptedModel({ responses: [spawn({}), say("Reviewed.")] }),
      subagents: [reviewer],
    });
    const result = await lead.run("Review the diff.", { store });
    expect(result).toMatchObject({ status: "completed", output: "Reviewed." });
    const log = await events(store, result.thread);
    const types = log.map((e) => e.type);
    const at = types.indexOf("agent_spawned");
    expect(types.slice(at, at + 3)).toEqual([
      "agent_spawned",
      "agent_finished",
      "tool_result",
    ]);
    const [spawned] = only(log, "agent_spawned");
    const [finished] = only(log, "agent_finished");
    expect(spawned?.data).toMatchObject({
      call_id: "c1",
      agent_name: "reviewer",
      mode: "foreground",
      isolation: "none",
      budget: { max_model_requests: 5 },
    });
    expect(finished?.data).toMatchObject({
      status: "completed",
      usage: { input_tokens: 10, output_tokens: 2 },
    });
    expect(only(log, "tool_result").at(0)?.data.preview).toBe("LGTM");
    const child = await childEvents(store, log);
    const [started] = only(child, "thread_started");
    expect<unknown>(started?.data.parent).toEqual({
      thread_id: result.thread.id,
      branch_id: result.thread.branch,
      event_id: spawned?.event_id ?? "",
      relation: "subagent",
    });
    // Narrowed: echo is the child's own tool, which its parent lacks.
    expect(started?.data.tools.map((t) => t.name)).toEqual([
      "read_tool_result",
      "send_message",
      "team_task_claim",
      "team_task_create",
      "team_task_update",
      "todo_write",
    ]);
    expect(
      only(child, "user_input").map((e) => [e.data.source, e.data.text]),
    ).toEqual([["parent_agent", "Review it."]]);
    const thread = unwrap(await openThread(store, result.thread.id));
    expect<unknown>(await thread.children()).toEqual([
      {
        child_thread_id: spawned?.data.child_thread_id ?? "",
        status: "completed",
      },
    ]);
  });

  test("an unknown agent or an unsupported isolation fails before agent_spawned", async () => {
    const store = sqlite(":memory:");
    const reviewer = agent({
      name: "reviewer",
      model: scriptedModel({ responses: [] }),
    });
    const lead = agent({
      model: scriptedModel({
        responses: [
          use("spawn_agent", { agent: "nobody", prompt: "x" }, "c1"),
          spawn({ isolation: "forked_sandbox" }, "c2"),
          say("ok"),
        ],
      }),
      subagents: [reviewer],
    });
    const result = await lead.run("go", { store });
    const log = await events(store, result.thread);
    expect(only(log, "agent_spawned")).toEqual([]);
    expect(
      only(log, "tool_result").map((e) => [
        e.data.call_id,
        e.data.origin,
        e.data.preview,
      ]),
    ).toEqual([
      ["c1", "not_executed", "unknown agent: nobody"],
      [
        "c2",
        "not_executed",
        "isolation forked_sandbox is not supported yet; use none",
      ],
    ]);
  });

  test("the parent's policy caps the child: a tool the parent denies is denied in the child", async () => {
    const store = sqlite(":memory:");
    const reviewer = agent({
      name: "reviewer",
      model: scriptedModel({
        responses: [use("echo", { text: "hi" }, "k1"), say("done")],
      }),
      tools: [echo],
      permissions: { allow: ["echo"] },
    });
    const lead = agent({
      model: scriptedModel({ responses: [spawn({}), say("ok")] }),
      tools: [echo],
      permissions: { deny: ["echo"] },
      subagents: [reviewer],
    });
    const result = await lead.run("go", { store });
    const child = await childEvents(store, await events(store, result.thread));
    expect(
      only(child, "permission_decision").map((e) => e.data.decision),
    ).toEqual(["deny"]);
  });
});

describe("spawn_agent in the background (F7.2)", () => {
  test("a deferred placeholder at once, then agent_finished and tool_result_late", async () => {
    const store = sqlite(":memory:");
    const scanner = agent({
      name: "scanner",
      model: scriptedModel({ responses: [say("No vulnerable deps.")] }),
    });
    const lead = agent({
      model: scriptedModel({
        responses: [
          use(
            "spawn_agent",
            { agent: "scanner", prompt: "Scan.", background: true },
            "c1",
          ),
          say("Scan started."),
        ],
      }),
      subagents: [scanner],
    });
    const result = await lead.run("go", { store });
    expect(result).toMatchObject({
      status: "completed",
      output: "Scan started.",
    });
    const log = await events(store, result.thread);
    expect(only(log, "tool_result").map((e) => e.data.origin)).toEqual([
      "deferred",
    ]);
    const types = log.map((e) => e.type);
    const at = types.indexOf("agent_finished");
    expect(types.slice(at)).toEqual(["agent_finished", "tool_result_late"]);
    expect(only(log, "tool_result_late")[0]?.data.preview).toBe(
      "No vulnerable deps.",
    );
  });
});

describe("subagent hooks", () => {
  test("subagent_start deny: a denied result and no child", async () => {
    const store = sqlite(":memory:");
    const reviewer = agent({
      name: "reviewer",
      model: scriptedModel({ responses: [] }),
    });
    const lead = agent({
      model: scriptedModel({ responses: [spawn({}), say("ok")] }),
      subagents: [reviewer],
      extensions: [
        extension({
          name: "gate",
          hooks: {
            subagentStart: async () => ({
              decision: "deny",
              reason: "no children",
            }),
          },
        }),
      ],
    });
    const result = await lead.run("go", { store });
    const log = await events(store, result.thread);
    expect(only(log, "agent_spawned")).toEqual([]);
    expect(only(log, "tool_result")[0]?.data).toMatchObject({
      origin: "denied",
      preview: "no children",
    });
  });

  test("subagent_stop continue sends the child one more input; one agent_finished", async () => {
    const store = sqlite(":memory:");
    const reviewer = agent({
      name: "reviewer",
      model: scriptedModel({ responses: [say("draft"), say("final")] }),
    });
    let stops = 0;
    const lead = agent({
      model: scriptedModel({ responses: [spawn({}), say("ok")] }),
      subagents: [reviewer],
      extensions: [
        extension({
          name: "check",
          hooks: {
            subagentStop: async () =>
              stops++ === 0
                ? { decision: "continue", reason: "Double-check it." }
                : { decision: "stop" },
          },
        }),
      ],
    });
    const result = await lead.run("go", { store });
    const log = await events(store, result.thread);
    expect(only(log, "agent_finished")).toHaveLength(1);
    expect(only(log, "tool_result")[0]?.data.preview).toBe("final");
    const child = await childEvents(store, log);
    expect(only(child, "user_input").map((e) => e.data.text)).toEqual([
      "Review it.",
      "Double-check it.",
    ]);
  });
});
