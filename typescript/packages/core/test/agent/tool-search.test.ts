import { beforeEach, describe, expect, test } from "bun:test";
import { agent, openThread, scriptedModel, sqlite } from "../../src";
import { openStore } from "../../src/agent/sqlite";
import { cacheBreaks } from "../../src/reduce/projections";
import { unwrap } from "../store/helpers";
import {
  addComment,
  createIssue,
  eventsOf,
  lookup,
  offered,
  ran,
  requestsOf,
  say,
  startedOf,
  use,
} from "./tool-search-kit";

// tool_search end to end (spec/schema/README.md, "Deferred tools and tool_search"): deferred
// tools are offered only after a search loads them, a load is a tools_loaded after the search's
// result, and the declared prefix (invariant 5) never changes.

const EXACT = { query: "create_issue, add_comment" };
const tools = [lookup, createIssue, addComment];
const BYPASS = { mode: "bypass" } as const;

beforeEach(() => {
  ran.length = 0;
});

describe("tool_search end to end", () => {
  test("a search loads exact names; the next request offers their schemas and a call runs", async () => {
    const store = sqlite(":memory:");
    const model = scriptedModel({
      responses: [
        use("tool_search", EXACT, "c1"),
        use("create_issue", { text: "Login fails" }, "c2"),
        say("Filed."),
      ],
    });
    const result = await agent({ model, tools, permissions: BYPASS }).run(
      "File a bug.",
      { store },
    );
    expect(result.status).toBe("completed");
    const events = await eventsOf(store, result.thread);
    const [first, second] = await requestsOf(store, events);
    if (first === undefined || second === undefined)
      throw new Error("two requests");
    expect(offered(first)).toEqual([
      "read_tool_result",
      "todo_write",
      "tool_search",
      "lookup_order",
    ]);
    const search = startedOf(events).data.tools.find(
      (t) => t.name === "tool_search",
    );
    expect(search?.description).toEndWith(
      "\n\nDeferred tools (search to load): add_comment, create_issue",
    );
    expect(offered(second)).toEqual([
      ...offered(first),
      "create_issue",
      "add_comment",
    ]);
    const at = events.findIndex((e) => e.type === "tools_loaded");
    const loaded = events[at];
    const before = events[at - 1];
    expect<unknown>(before?.type === "tool_result" && before.data.call_id).toBe(
      "c1",
    );
    const pinned = new Map<string, unknown>(
      startedOf(events).data.tools.map((t) => [t.name, t.spec_ref]),
    );
    expect<unknown>(loaded?.type === "tools_loaded" && loaded.data).toEqual({
      call_id: "c1",
      tools: [
        { name: "create_issue", spec_ref: pinned.get("create_issue") },
        { name: "add_comment", spec_ref: pinned.get("add_comment") },
      ],
    });
    expect(ran).toEqual(["create_issue"]);
  });

  test("calling a deferred tool before a load fails before any effect", async () => {
    const store = sqlite(":memory:");
    const model = scriptedModel({
      responses: [use("create_issue", { text: "x" }, "c1"), say("Sorry.")],
    });
    const result = await agent({ model, tools }).run("File a bug.", { store });
    const events = await eventsOf(store, result.thread);
    const failed = events.find((e) => e.type === "tool_result");
    expect(failed?.type === "tool_result" && failed.data).toMatchObject({
      is_error: true,
      origin: "not_executed",
      preview: "tool_not_loaded: create_issue; find it with tool_search first",
    });
    expect(events.some((e) => e.type === "effect_begin")).toBe(false);
    expect(ran).toEqual([]);
  });

  test("two searches in one response run in order: the second sees the first's load", async () => {
    const store = sqlite(":memory:");
    const both = {
      content: [
        {
          type: "tool_use",
          call_id: "c1",
          name: "tool_search",
          input: { query: "create_issue" },
        },
        {
          type: "tool_use",
          call_id: "c2",
          name: "tool_search",
          input: { query: "create_issue" },
        },
      ],
      stop_reason: "tool_use",
      usage: { input_tokens: 10, output_tokens: 2 },
    };
    const model = scriptedModel({ responses: [both, say("ok")] });
    const result = await agent({ model, tools }).run("Go.", { store });
    const events = await eventsOf(store, result.thread);
    const previews = events.flatMap((e) =>
      e.type === "tool_result" ? [e.data.preview] : [],
    );
    expect(previews).toEqual([
      "create_issue: Create a Jira issue.",
      "create_issue: already loaded",
    ]);
    expect(events.filter((e) => e.type === "tools_loaded")).toHaveLength(1);
  });

  test("invalid input loads nothing", async () => {
    const store = sqlite(":memory:");
    const bad = [
      {},
      { query: "" },
      { query: "x".repeat(201) },
      { query: "jira", limit: 0 },
      { query: "jira", limit: 11 },
      { query: "jira", limit: 1.5 },
    ];
    const model = scriptedModel({
      responses: [
        ...bad.map((q, i) => use("tool_search", q, `c${i}`)),
        say("ok"),
      ],
    });
    const result = await agent({ model, tools }).run("Go.", { store });
    const events = await eventsOf(store, result.thread);
    const results = events.flatMap((e) =>
      e.type === "tool_result" ? [e] : [],
    );
    expect(results.map((e) => [e.data.is_error, e.data.origin])).toEqual(
      bad.map(() => [true, "not_executed"]),
    );
    expect(events.some((e) => e.type === "tools_loaded")).toBe(false);
    // 200 code points is the limit, counted as code points: 200 astral characters pass.
    const store2 = sqlite(":memory:");
    const ok = scriptedModel({
      responses: [
        use("tool_search", { query: "😀".repeat(200) }, "c1"),
        say("ok"),
      ],
    });
    const run2 = await agent({ model: ok, tools }).run("Go.", {
      store: store2,
    });
    const got = (await eventsOf(store2, run2.thread)).find(
      (e) => e.type === "tool_result",
    );
    expect(got?.type === "tool_result" && got.data.preview).toBe(
      "no deferred tool matches",
    );
  });
});

describe("invariant 5 on recorded bytes", () => {
  test("line 0 is byte-equal across two loads, each request extends the last, replay holds", async () => {
    const store = sqlite(":memory:");
    const model = scriptedModel({
      responses: [
        use("tool_search", { query: "create_issue" }, "c1", 9000),
        say("Loaded.", 9000),
        use("tool_search", { query: "comment" }, "c2", 9000),
        say("Loaded again.", 500),
      ],
    });
    const bot = agent({ model, tools, context: { cache_ttl_ms: 3_600_000 } });
    const first = await bot.run("Load create.", { store });
    const second = await bot.run("Load comment.", {
      store,
      thread: first.thread,
    });
    expect(second.status).toBe("completed");
    const events = await eventsOf(store, second.thread);
    const prefixes = new Set(
      events.flatMap((e) =>
        e.type === "model_request" ? [e.data.declared_prefix.sha256] : [],
      ),
    );
    expect(prefixes.size).toBe(1);
    expect(events.some((e) => e.type === "settings_changed")).toBe(false);
    const requests = await requestsOf(store, events);
    for (const [i, bytes] of requests.entries()) {
      const previous = requests[i - 1];
      if (previous !== undefined)
        expect(bytes.subarray(0, previous.length)).toEqual(previous);
    }
    const thread = unwrap(await openThread(store, second.thread.id));
    expect((await thread.replay()).ok).toBe(true);
    const breaks = cacheBreaks(events, 3_600_000);
    expect(breaks.map((b) => b.likely_cause)).toEqual(["tools_loaded"]);
  });
});

describe("the pin", () => {
  test("deferred tools are pinned in reference form with their spec artifacts stored", async () => {
    const store = sqlite(":memory:");
    const result = await agent({
      model: scriptedModel({ responses: [say("hi")] }),
      tools,
    }).run("hi", { store });
    const events = await eventsOf(store, result.thread);
    const specs = startedOf(events).data.tools;
    const create = specs.find((t) => t.name === "create_issue");
    expect(create).toMatchObject({
      defer_loading: true,
      effect_class: "unguarded",
    });
    expect(create?.input_schema).toBeUndefined();
    const { artifacts } = await openStore(store);
    const ref = create?.spec_ref;
    if (ref === undefined) throw new Error("reference form");
    const bytes = unwrap(artifacts.get(ref.sha256));
    expect(JSON.parse(new TextDecoder().decode(bytes))).toEqual(
      createIssue.spec(),
    );
  });

  test("never pins every tool inline with no tool_search; always defers every user tool", async () => {
    const names = async (deferTools: "never" | "always") => {
      const store = sqlite(":memory:");
      const result = await agent({
        model: scriptedModel({ responses: [say("hi")] }),
        tools,
        context: { defer_tools: deferTools },
      }).run("hi", { store });
      return startedOf(await eventsOf(store, result.thread)).data.tools.map(
        (t) => [t.name, t.defer_loading === true],
      );
    };
    expect(await names("never")).toEqual([
      ["read_tool_result", false],
      ["todo_write", false],
      ["lookup_order", false],
      ["create_issue", false],
      ["add_comment", false],
    ]);
    expect(await names("always")).toEqual([
      ["read_tool_result", false],
      ["todo_write", false],
      ["tool_search", false],
      ["lookup_order", true],
      ["create_issue", true],
      ["add_comment", true],
    ]);
  });

  test("a changed deferred schema changes spec_ref and config_hash, so the thread fails closed", async () => {
    const store = sqlite(":memory:");
    const first = await agent({
      model: scriptedModel({ responses: [say("hi")] }),
      tools,
    }).run("hi", { store });
    const { deferred } = await import("./tool-search-kit");
    const changed = deferred("create_issue", "Create a Jira issue, v2.");
    const again = agent({
      model: scriptedModel({ responses: [say("hi")] }),
      tools: [lookup, changed, addComment],
    });
    await expect(
      again.run("hi", { store, thread: first.thread }),
    ).rejects.toThrow(/config/);
  });

  test("defer with endsTurn, or a tool named tool_search next to a deferred one, is a setup error", async () => {
    const { tool } = await import("../../src");
    const { z } = await import("zod");
    const ends = tool({
      name: "finish",
      description: "Finish.",
      input: z.object({}),
      runs: "host",
      endsTurn: true,
      defer: true,
      execute: async () => "done",
    });
    expect(() => ends.spec()).toThrow(/defer/);
    const clash = tool({
      name: "tool_search",
      description: "Mine.",
      input: z.object({}),
      runs: "host",
      execute: async () => "x",
    });
    const bot = agent({
      model: scriptedModel({ responses: [] }),
      tools: [clash, createIssue],
    });
    expect(await bot.check()).toMatchObject({
      ok: false,
      error: { code: "invalid_config" },
    });
  });
});
