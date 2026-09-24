import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { recordingFetch, renderBody, sse } from "@threads/adapter-testkit";
import { agent, sqlite } from "@threads/core";
import {
  type Json,
  markTestKit,
  memoryContext,
  parseRender,
} from "@threads/core/adapter";
import { z } from "zod";
import { type AnthropicOptions, anthropic } from "../src";
import { toAnthropic } from "../src/request";

// The request bodies both languages build (spec/conformance/vectors/anthropic-requests.json),
// the cache controls' placement, and line 0 staying byte-stable through a cached run.

const Vector = z.object({
  artifacts: z.record(z.string(), z.string()),
  cases: z.array(
    z.object({ name: z.string(), render: z.string(), body: z.unknown() }),
  ),
});
const vector = Vector.parse(
  JSON.parse(
    readFileSync(
      new URL(
        "../../../../spec/conformance/vectors/anthropic-requests.json",
        import.meta.url,
      ),
      "utf8",
    ),
  ),
);
const encoder = new TextEncoder();

async function map(render: Uint8Array) {
  const context = memoryContext();
  for (const data of Object.values(vector.artifacts))
    await context.put(Uint8Array.fromBase64(data), "text/plain");
  return toAnthropic(parseRender(render), context);
}

describe("shared request vector: the same canonical JSON in both languages", () => {
  for (const c of vector.cases)
    test(c.name, async () => {
      const mapped = await map(encoder.encode(c.render));
      if (!mapped.ok) throw new Error(mapped.message);
      // Deep equality of parsed JSON is canonical-JSON equality: key order is ignored.
      expect(JSON.parse(JSON.stringify(mapped.body))).toEqual(c.body);
    });
});

const head = (settings: { readonly [key: string]: Json }) => ({
  adapter: { name: "anthropic", version: "1", settings },
  model: { provider: "anthropic", name: "claude-sonnet-5" },
  params: { max_tokens: 1024 },
  system: "",
  tools: [],
});
const hi = { role: "user", content: [{ type: "text", text: "hi" }] };

describe("placement", () => {
  test("no system and no tools: automatic caching only", async () => {
    const mapped = await map(renderBody([head({ prompt_cache: "5m" }), hi]));
    if (!mapped.ok) throw new Error(mapped.message);
    expect(mapped.body["cache_control"]).toEqual({ type: "ephemeral" });
    expect(mapped.body["system"]).toBeUndefined();
    expect(mapped.body["tools"]).toBeUndefined();
  });

  test.each(["10m", true])(
    "a line 0 with prompt_cache %p is refused before dispatch",
    async (ttl) => {
      const mapped = await map(renderBody([head({ prompt_cache: ttl }), hi]));
      expect(mapped.ok || mapped.code).toBe("continuation_unsupported");
    },
  );
});

const ev = (data: { readonly type: string } & { [key: string]: Json }) => ({
  event: data.type,
  data,
});
const reply = (text: string, usage: { readonly [key: string]: Json }) =>
  sse([
    ev({ type: "message_start", message: { usage } }),
    ev({
      type: "content_block_start",
      index: 0,
      content_block: { type: "text", text },
    }),
    ev({ type: "content_block_stop", index: 0 }),
    ev({
      type: "message_delta",
      delta: { stop_reason: "end_turn" },
      usage: { output_tokens: 2 },
    }),
    ev({ type: "message_stop" }),
  ]);
const plain = { input_tokens: 9 };

function bot(responses: Response[], options: AnthropicOptions = {}) {
  const { fetch, calls } = recordingFetch(responses);
  const model = anthropic("claude-sonnet-5", {
    apiKey: "test-key",
    fetch,
    ...options,
  });
  markTestKit(model);
  return { bot: agent({ instructions: "Answer briefly.", model }), calls };
}

const Body = z.object({
  system: z.unknown(),
  messages: z.array(z.unknown()),
  cache_control: z.unknown().optional(),
});

describe("a cached run", () => {
  test("line 0 is byte-stable and turn 1's request prefixes turn 2's", async () => {
    const { bot: a, calls } = bot([reply("One.", plain), reply("Two.", plain)]);
    const first = await a.run("hi", { store: sqlite(":memory:") });
    const second = await a.run("again", { thread: first.thread });
    expect(second.status).toBe("completed");
    // replay re-renders every request and checks C7 per settings epoch.
    expect(await second.thread.replay()).toEqual({
      ok: true,
      value: undefined,
    });
    const [one, two] = calls.map((c) => Body.parse(c.body));
    expect(two?.system).toEqual(one?.system);
    expect(two?.cache_control).toEqual({ type: "ephemeral" });
    expect(two?.messages.slice(0, one?.messages.length)).toEqual(
      one?.messages ?? [],
    );
  });

  test("a thread started before caching continues only with promptCache false", async () => {
    const off = { promptCache: false } as const;
    const seeded = bot([reply("One.", plain)], off);
    const first = await seeded.bot.run("hi", { store: sqlite(":memory:") });
    const cached = bot([reply("Two.", plain)]);
    await expect(
      cached.bot.run("again", { thread: first.thread }),
    ).rejects.toThrow("another config");
    const same = bot([reply("Two.", plain)], off);
    const next = await same.bot.run("again", { thread: first.thread });
    expect(next.status).toBe("completed");
    expect(Body.parse(same.calls[0]?.body).cache_control).toBeUndefined();
  });

  test("mixed-TTL writes leave the attempt's cost incomplete", async () => {
    const usage = {
      input_tokens: 9,
      cache_creation_input_tokens: 248,
      cache_creation: {
        ephemeral_5m_input_tokens: 148,
        ephemeral_1h_input_tokens: 100,
      },
    };
    const price = { input: 3000, output: 15_000, cache_read: 300 };
    const { bot: a } = bot([reply("One.", usage)], {
      promptCache: "1h",
      price,
    });
    const result = await a.run("hi", { store: sqlite(":memory:") });
    expect(await result.thread.cost()).toMatchObject({
      ok: true,
      value: { complete: false },
    });
  });
});
