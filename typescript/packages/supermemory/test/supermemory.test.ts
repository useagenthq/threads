import { describe, expect, test } from "bun:test";
import { agent, scriptedModel, secret, sqlite } from "@threads/core";
import {
  dispatched,
  type Fetch,
  memoryProviderSuite,
} from "@threads/core/adapter";
import { z } from "zod";
import { supermemory } from "../src";

// supermemory() against an in-memory stand-in for the Supermemory HTTP API (no network): the
// shared provider suite, the fence at the SDK's fetch, and the swap through the agent (F3.5).

type Doc = { id: string; content: string; tags: string[]; metadata: unknown };

const Body = z.object({
  q: z.string().default(""),
  containerTags: z.array(z.string()).default([]),
  content: z.string().default(""),
  containerTag: z.string().default(""),
  metadata: z.unknown().optional(),
});

function search(docs: readonly Doc[], q: string, tags: readonly string[]) {
  const words = q.toLowerCase().split(/\W+/).filter(Boolean);
  return docs
    .filter((d) => d.tags.some((t) => tags.includes(t)))
    .filter((d) => words.some((w) => d.content.toLowerCase().includes(w)))
    .map((d) => ({
      documentId: d.id,
      score: 0.9,
      content: d.content,
      chunks: [{ content: d.content, isRelevant: true, score: 0.9 }],
      metadata: d.metadata,
      createdAt: "2026-01-01T00:00:00Z",
      updatedAt: "2026-01-01T00:00:00Z",
      title: null,
      type: "text",
    }));
}

function backend(): { fetch: Fetch; requests: string[] } {
  const docs = new Map<string, Doc>();
  const requests: string[] = [];
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    });
  const fetch: Fetch = async (input, init) => {
    const request = new Request(input, init);
    const { pathname } = new URL(request.url);
    requests.push(`${request.method} ${pathname}`);
    if (request.method !== "POST") {
      const id = pathname.split("/").at(-1) ?? "";
      const doc = docs.get(id);
      if (doc === undefined) return json({ error: "not found" }, 404);
      if (request.method === "GET") return json({ ...doc, customId: null });
      docs.delete(id);
      return new Response(null, { status: 204 });
    }
    const body = Body.parse(await request.json());
    if (pathname === "/v3/search") {
      const results = search([...docs.values()], body.q, body.containerTags);
      return json({ results, timing: 1, total: results.length });
    }
    const id = `doc${docs.size + 1}`;
    docs.set(id, {
      id,
      content: body.content,
      tags: [body.containerTag],
      metadata: body.metadata,
    });
    return json({ id, status: "queued" });
  };
  return { fetch, requests };
}

process.env["THREADS_TEST_SUPERMEMORY_KEY"] = "sm-test-key";
const key = secret("THREADS_TEST_SUPERMEMORY_KEY");

memoryProviderSuite(
  "supermemory",
  async () => supermemory({ apiKey: key, fetch: backend().fetch }),
  { test: (name, body) => test(name, body) },
);

describe("supermemory transport", () => {
  test("a stale lease sends nothing: the fence is at the SDK's fetch", async () => {
    const { fetch, requests } = backend();
    const p = supermemory({ apiKey: key, fetch });
    const stale = {
      fence: async () =>
        ({
          ok: false,
          error: { code: "stale_epoch", message: "lease lost" },
        }) as const,
    };
    const d = await dispatched(stale, () =>
      p.recall({ tenant_id: "t", agent: "a", scope: "s" }, "anything"),
    );
    expect({ sent: d.sent, refused: d.refused }).toEqual({
      sent: false,
      refused: true,
    });
    expect(requests).toEqual([]);
  });

  test("a missing key is a setup error", async () => {
    const checked = await agent({
      model: scriptedModel({ responses: [] }),
      memory: supermemory({ apiKey: secret("THREADS_TEST_NO_SUCH_KEY") }),
    }).check();
    expect(checked).toMatchObject({
      ok: false,
      error: { code: "missing_secret" },
    });
  });

  test("swapped in by one config entry: saves are effects, recall is injected (F3.5)", async () => {
    const usage = { input_tokens: 1, output_tokens: 1 };
    const turn = (content: unknown[], stop: string) => ({
      content,
      stop_reason: stop,
      usage,
    });
    const { fetch } = backend();
    const memory = supermemory({ apiKey: key, fetch });
    const store = sqlite(":memory:");
    const bot = (responses: unknown[]) =>
      agent({
        model: scriptedModel({ responses }),
        memory,
        memoryWrite: "allow",
      });
    await bot([
      turn(
        [
          {
            type: "tool_use",
            call_id: "c1",
            name: "save_memory",
            input: { text: "the build uses bun" },
          },
        ],
        "tool_use",
      ),
      turn([{ type: "text", text: "ok" }], "end_turn"),
    ]).run("remember the build uses bun", { store });
    const later = await bot([
      turn(
        [
          {
            type: "tool_use",
            call_id: "c2",
            name: "search_memory",
            input: { query: "build" },
          },
        ],
        "tool_use",
      ),
      turn([{ type: "text", text: "bun" }], "end_turn"),
    ]).run("what does the build use?", { store });
    expect(later).toMatchObject({ status: "completed", output: "bun" });
  });
});
