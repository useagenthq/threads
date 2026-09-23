import { describe, expect, test } from "bun:test";
import {
  type AgentOptions,
  agent,
  fakeSandbox,
  scriptedModel,
  secret,
  sqlite,
} from "../../src";
import { openStore } from "../../src/agent/sqlite";
import type { KnownEvent } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { refReader, verifyRequests } from "../../src/render";
import type { WebTransport } from "../../src/tools/web-transport";
import { unwrap } from "../store/helpers";

// The gated built-ins in an agent: pinned only when configured,
// refused at setup when their capability is missing, and decided by the permission rules
// (web counts as "other"; domain rules apply) before dispatch.

const usage = { input_tokens: 1, output_tokens: 1 };
const fetchCall = (url: string) => ({
  content: [
    { type: "tool_use", call_id: "c1", name: "web_fetch", input: { url } },
  ],
  stop_reason: "tool_use",
  usage,
});
const end = {
  content: [{ type: "text", text: "ok" }],
  stop_reason: "end_turn",
  usage,
};

function net(): WebTransport & { readonly fetched: string[] } {
  const fetched: string[] = [];
  return {
    fetched,
    resolve: async () => ["93.184.216.34"],
    fetch: async (url) => {
      fetched.push(url);
      return new Response("the page", {
        headers: { "content-type": "text/plain" },
      });
    },
  };
}

async function run(
  permissions: NonNullable<AgentOptions<undefined, string>["permissions"]>,
  t: WebTransport,
): Promise<readonly KnownEvent[]> {
  const result = await agent({
    model: scriptedModel({
      responses: [fetchCall("https://docs.example.com/a"), end],
    }),
    web: { fetch: true, transport: t },
    permissions,
  }).run("read it", { store: sqlite(":memory:") });
  const { log, artifacts } = await openStore(result.thread.store);
  const events = knownEvents(unwrap(log.read(result.thread.branch)));
  unwrap(verifyRequests(events, refReader(artifacts)));
  return events;
}

describe("gated built-ins in an agent", () => {
  test("only configured ones are pinned, sorted with the built-ins", async () => {
    const names = async (
      options: Omit<AgentOptions<undefined, string>, "model" | "output">,
    ) => {
      const result = await agent({
        model: scriptedModel({ responses: [end] }),
        ...options,
      }).run("hi", {
        store: sqlite(":memory:"),
      });
      const { log } = await openStore(result.thread.store);
      const started = knownEvents(unwrap(log.read(result.thread.branch))).find(
        (e) => e.type === "thread_started",
      );
      return started?.type === "thread_started"
        ? started.data.tools.map((t) => `${t.name}:${t.effect_class}`)
        : [];
    };
    expect(await names({})).toEqual([
      "read_tool_result:read_only",
      "todo_write:read_only",
    ]);
    const search = { search: async () => ({ ok: true as const, value: [] }) };
    expect(await names({ web: { fetch: true, search } })).toEqual([
      "read_tool_result:read_only",
      "todo_write:read_only",
      "web_fetch:read_only",
      "web_search:read_only",
    ]);
    const git = await names({
      sandbox: fakeSandbox(),
      git: { credential: secret("THREADS_UNSET_TOKEN") },
    });
    expect(git.filter((n) => n.includes("git") || n.includes("pull"))).toEqual([
      "git_clone:read_only",
      "git_fetch:read_only",
      "git_push:reconcilable",
      "open_pull_request:reconcilable",
    ]);
  });

  test("git without a sandbox is capability_missing", async () => {
    const model = scriptedModel({ responses: [] });
    await expect(
      agent({ model, git: { credential: secret("X") } }).check(),
    ).resolves.toMatchObject({
      ok: false,
      error: { code: "capability_missing" },
    });
  });

  test("web_fetch asks in default mode (web is other), and nothing is fetched before approval", async () => {
    const t = net();
    const events = await run({ mode: "default" }, t);
    const decision = events.find((e) => e.type === "permission_decision");
    expect(
      decision?.type === "permission_decision" && decision.data.decision,
    ).toBe("ask");
    expect(t.fetched).toEqual([]);
  });

  test("a domain deny rule refuses before dispatch; a domain allow rule fetches and records a citation", async () => {
    const denied = net();
    await run(
      {
        mode: "bypass",
        allow_bypass: true,
        deny: ["web_fetch(domain:*.example.com)"],
      },
      denied,
    );
    expect(denied.fetched).toEqual([]);
    const allowed = net();
    const events = await run(
      { mode: "default", allow: ["web_fetch(domain:docs.example.com)"] },
      allowed,
    );
    expect(allowed.fetched).toEqual(["https://docs.example.com/a"]);
    const shown = events.find((e) => e.type === "tool_result");
    if (shown?.type !== "tool_result") throw new Error("no result");
    expect(shown.data.content?.map((p) => p.type)).toEqual([
      "text",
      "citation",
    ]);
    expect(shown.data.content?.[1]).toMatchObject({
      source_kind: "web",
      source_id: "https://docs.example.com/a",
    });
    // read_only: no effect events.
    expect(events.some((e) => e.type === "effect_begin")).toBe(false);
  });
});
