import { describe, expect, test } from "bun:test";
import { agent, ConfigError, scriptedModel, sqlite } from "../../src";
import { openStore, storeConnection } from "../../src/agent/sqlite";
import type { BranchId, KnownEvent } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { unwrap } from "../store/helpers";

// spec/schema/README.md, Budget enforcement and Handoff scope: a limit is never skipped for want
// of a per-attempt bound, the ledger is rebuilt from the log, and a handoff target is covered by
// every budget that covered the thread handing off.

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
const todo = (id: string) =>
  use("todo_write", { todos: [{ id, content: id, status: "pending" }] }, id);

type Store = ReturnType<typeof sqlite>;

async function events(
  store: Store,
  branch: BranchId,
): Promise<readonly KnownEvent[]> {
  const { log } = await openStore(store);
  return knownEvents(unwrap(log.read(branch)));
}

const requests = (log: readonly KnownEvent[]) =>
  log.filter((e) => e.type === "model_request").length;

/** A scripted model whose epoch pins no max_tokens: its output per attempt is unbounded. */
function unbounded(responses: readonly unknown[]) {
  const m = scriptedModel({ responses });
  return { ...m, info: { ...m.info, params: {} } };
}

describe("budget coverage", () => {
  test("setup refuses a token limit the model has no per-attempt bound for", async () => {
    const a = agent({
      budget: { max_output_tokens: 150 },
      model: unbounded([say("done")]),
    });
    expect(await a.check()).toMatchObject({
      ok: false,
      error: { code: "budget_unenforceable" },
    });
    expect(a.run("go", { store: sqlite(":memory:") })).rejects.toBeInstanceOf(
      ConfigError,
    );
  });

  test("a run budget the model can't bound is refused at run start", async () => {
    const a = agent({ model: unbounded([say("done")]) });
    expect(
      a.run("go", {
        store: sqlite(":memory:"),
        budget: { max_output_tokens: 150 },
      }),
    ).rejects.toMatchObject({ code: "budget_unenforceable" });
  });

  test("onUnknownUsage stop passes setup and refuses the unbounded attempt at run time", async () => {
    const store = sqlite(":memory:");
    const a = agent({
      budget: { max_output_tokens: 150 },
      onUnknownUsage: "stop",
      model: unbounded([say("never")]),
    });
    expect(await a.check()).toEqual({ ok: true, value: undefined });
    const result = await a.run("go", { store });
    expect(result).toMatchObject({
      status: "budget_exhausted",
      budget: {
        limit: "max_output_tokens",
        observed: 0,
        observed_is_upper_bound: true,
      },
    });
    const log = await events(store, result.thread.branch);
    expect(requests(log)).toBe(0);
    const started = log.find((e) => e.type === "thread_started");
    expect(started?.data.policy?.on_unknown_usage).toBe("stop");
  });

  test("onUnknownUsage stop lets a run budget the model can't bound start", async () => {
    const a = agent({ onUnknownUsage: "stop", model: unbounded([say("x")]) });
    const result = await a.run("go", {
      store: sqlite(":memory:"),
      budget: { max_output_tokens: 150 },
    });
    expect(result).toMatchObject({
      status: "budget_exhausted",
      budget: { scope: "run", limit: "max_output_tokens" },
    });
  });

  test("onUnknownUsage upper_bound keeps the setup refusal and is pinned", async () => {
    const a = agent({
      budget: { max_output_tokens: 150 },
      onUnknownUsage: "upper_bound",
      model: unbounded([say("done")]),
    });
    expect(await a.check()).toMatchObject({
      ok: false,
      error: { code: "budget_unenforceable" },
    });
  });

  test("an inherited limit with no bound refuses the attempt instead of skipping it", async () => {
    const store = sqlite(":memory:");
    const worker = agent({
      name: "worker",
      model: unbounded([say("child done")]),
    });
    const lead = agent({
      model: scriptedModel({
        responses: [
          use("spawn_agent", { agent: "worker", prompt: "Work." }, "s1"),
          say("ok"),
        ],
      }),
      subagents: [worker],
      budget: { max_output_tokens: 5000 },
    });
    const result = await lead.run("go", { store });
    const lead_log = await events(store, result.thread.branch);
    const spawned = lead_log.find((e) => e.type === "agent_spawned");
    if (spawned?.type !== "agent_spawned") throw new Error("no child");
    const { log } = await openStore(store);
    const child = await events(
      store,
      unwrap(log.mainBranch(spawned.data.child_thread_id)),
    );
    expect(requests(child)).toBe(0);
    expect(
      child.flatMap((e) => (e.type === "budget_exceeded" ? [e.data] : [])),
    ).toEqual([
      {
        scope: "ancestor",
        owner_thread_id: result.thread.id,
        limit: "max_output_tokens",
        limit_value: 5000,
        observed: 2,
        observed_is_upper_bound: true,
      },
    ]);
  });

  test("a wiped ledger is rebuilt from the log, never reset", async () => {
    const store = sqlite(":memory:");
    const a = agent({
      budget: { max_model_requests: 2 },
      model: scriptedModel({ responses: [say("1"), say("2"), say("3")] }),
    });
    const r1 = await a.run("one", { store });
    await a.run("two", { store, thread: r1.thread.id });
    const { db } = await storeConnection(store);
    db.run("DELETE FROM budget_ledger", []);
    const r3 = await a.run("three", { store, thread: r1.thread.id });
    expect(r3).toMatchObject({
      status: "budget_exhausted",
      budget: { limit: "max_model_requests", observed: 3 },
    });
  });

  test("a handoff target is covered by the source's thread and run budgets", async () => {
    const store = sqlite(":memory:");
    const target = agent({
      name: "target",
      model: scriptedModel({
        responses: [todo("t1"), todo("t2"), todo("t3"), say("spent")],
      }),
    });
    const source = agent({
      name: "source",
      budget: { max_model_requests: 2 },
      model: scriptedModel({
        responses: [use("handoff", { agent: "target" }, "h1")],
      }),
      handoffs: [target],
    });
    const r = await source.run("help", {
      store,
      budget: { max_model_requests: 2 },
    });
    if (r.status !== "handed_off") throw new Error(r.status);
    const to = await events(store, r.to_thread.branch);
    expect(requests(to)).toBe(1);
    expect(
      to.flatMap((e) => (e.type === "budget_exceeded" ? [e.data] : [])),
    ).toMatchObject([
      { scope: "ancestor", owner_thread_id: r.thread.id, observed: 3 },
    ]);
  });
});
