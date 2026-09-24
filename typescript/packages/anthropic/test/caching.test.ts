import { describe, expect, test } from "bun:test";
import {
  drain,
  recordingFetch,
  renderBody,
  sse,
} from "@threads/adapter-testkit";
import { type Json, memoryContext } from "@threads/core/adapter";
import { type AnthropicOptions, anthropic } from "../src";

// Prompt caching at the factory (lane 10): the pin, the declared lifetime, the refusals that keep
// one TTL per request, the derived cache-write price, and the usage that can't be priced.

const key = { apiKey: "test-key" } as const;
const make = (options: AnthropicOptions = {}) =>
  anthropic("claude-sonnet-5", { ...key, ...options });
const search = { type: "web_search_20250305", name: "web_search" };
const configError = (message: string) =>
  expect.objectContaining({ code: "invalid_config", message });

describe("promptCache pins line 0 and declares the cache lifetime", () => {
  test('the default is "5m": pinned, and a 5-minute lifetime', () => {
    const { info } = make();
    expect(info.adapter.settings).toEqual({ prompt_cache: "5m" });
    expect(info.cache).toEqual({ ttl_ms: 300_000 });
  });

  test('"1h" pins 1h and a one-hour lifetime', () => {
    const { info } = make({ promptCache: "1h" });
    expect(info.adapter.settings).toEqual({ prompt_cache: "1h" });
    expect(info.cache).toEqual({ ttl_ms: 3_600_000 });
  });

  test("false pins nothing, so it continues a thread started before caching", () => {
    const { info } = make({ promptCache: false });
    expect(info.adapter.settings).toEqual({});
    expect(info.cache).toBe("none");
  });

  test("citations are pinned when on", () => {
    expect(make({ citations: true }).info.adapter.settings).toEqual({
      prompt_cache: "5m",
      citations: true,
    });
  });
});

describe("one source of cache controls, one TTL per request", () => {
  test.each([
    ["params", { params: { cache_control: { type: "ephemeral" } } }],
    [
      "params",
      { params: { metadata: [{ nested: { cache_control: { ttl: "1h" } } }] } },
    ],
    [
      "hostedTools",
      {
        hostedTools: [{ ...search, cache_control: { type: "ephemeral" } }],
      },
    ],
  ] satisfies [string, AnthropicOptions][])(
    "cache_control in %s is refused",
    (where, options) => {
      expect(() => make(options)).toThrow(
        configError(
          `anthropic ${where} can't set cache_control: pass promptCache`,
        ),
      );
    },
  );

  test('"1h" with a hosted tool is refused, naming both options', () => {
    expect(() => make({ promptCache: "1h", hostedTools: [search] })).toThrow(
      configError(
        "promptCache 1h can't be combined with hostedTools: Anthropic caches their results for 5 minutes, so writes would be billed at two rates",
      ),
    );
    expect(() => make({ hostedTools: [search] })).not.toThrow();
  });

  test("a value outside the option's type is refused", () => {
    const bad: unknown = "10m";
    expect(() =>
      anthropic("claude-sonnet-5", {
        ...key,
        // @ts-expect-error: an untyped caller passes a TTL the provider doesn't offer
        promptCache: bad,
      }),
    ).toThrow(
      configError(
        'anthropic promptCache must be "5m", "1h" or false, not "10m"',
      ),
    );
  });

  test.each([
    { output_format: { type: "json_schema" } },
    { output_config: { format: { type: "json_schema" } } },
  ])("citations with native structured output are refused", (params) => {
    expect(() => make({ citations: true, params })).toThrow(
      expect.objectContaining({ code: "invalid_config" }),
    );
  });

  test("citations with other output_config fields are fine", () => {
    const params = { output_config: { effort: "high" } };
    expect(() => make({ citations: true, params })).not.toThrow();
  });
});

describe("the cache-write price follows the pinned TTL", () => {
  const price = { input: 3000, output: 15_000, cache_read: 300 };

  test("derived from input: 1.25 x for 5m, 2 x for 1h", () => {
    expect(make({ price }).info.limits.price?.cache_write).toBe(3750);
    const hour = make({ price, promptCache: "1h" });
    expect(hour.info.limits.price?.cache_write).toBe(6000);
  });

  test("rounded up to whole nano-units", () => {
    const odd = { input: 3, output: 15 };
    expect(make({ price: odd }).info.limits.price?.cache_write).toBe(4);
  });

  test("an explicit cacheWrite wins, and no caching derives nothing", () => {
    const given = { ...price, cache_write: 4000 };
    expect(make({ price: given }).info.limits.price?.cache_write).toBe(4000);
    const off = make({ price, promptCache: false });
    expect(off.info.limits.price?.cache_write).toBeUndefined();
  });

  test("a missing cacheRead is 0.1 x input, rounded up, so reads are never free", () => {
    const bare = { input: 3001, output: 15_000 };
    expect(make({ price: bare }).info.limits.price?.cache_read).toBe(301);
    expect(make({ price }).info.limits.price?.cache_read).toBe(300);
    const off = make({ price: bare, promptCache: false });
    expect(off.info.limits.price?.cache_read).toBeUndefined();
  });
});

const ev = (data: { readonly type: string } & { [key: string]: Json }) => ({
  event: data.type,
  data,
});

/** One text reply whose usage reports `writes` cache-creation tokens split by TTL. */
async function writes(
  pinned: "5m" | "1h",
  split: {
    readonly ephemeral_5m_input_tokens: number;
    readonly ephemeral_1h_input_tokens: number;
  },
) {
  const head = {
    adapter: {
      name: "anthropic",
      version: "1",
      settings: { prompt_cache: pinned },
    },
    model: { provider: "anthropic", name: "claude-sonnet-5" },
    params: { max_tokens: 1024 },
    system: "Be brief.",
    tools: [],
  };
  const usage = {
    input_tokens: 5,
    cache_read_input_tokens: 0,
    cache_creation_input_tokens:
      split.ephemeral_5m_input_tokens + split.ephemeral_1h_input_tokens,
    cache_creation: split,
  };
  const { fetch } = recordingFetch([
    sse([
      ev({ type: "message_start", message: { id: "m", usage } }),
      ev({
        type: "content_block_start",
        index: 0,
        content_block: { type: "text", text: "ok" },
      }),
      ev({ type: "content_block_stop", index: 0 }),
      ev({
        type: "message_delta",
        delta: { stop_reason: "end_turn" },
        usage: { output_tokens: 1 },
      }),
      ev({ type: "message_stop" }),
    ]),
  ]);
  const request = {
    request_id: "b:e1",
    body: renderBody([
      head,
      { role: "user", content: [{ type: "text", text: "hi" }] },
    ]),
  };
  const { chunks } = await drain(
    make({ fetch }).send(request, memoryContext()),
  );
  const done = chunks.at(-1);
  if (done?.kind !== "done") throw new Error("no done chunk");
  return done.usage.cache_write_tokens;
}

describe("cache writes are priced at the pinned TTL or recorded unknown", () => {
  test("5m writes on a 5m pin (a hosted search result included) are recorded", async () => {
    const split = {
      ephemeral_5m_input_tokens: 900,
      ephemeral_1h_input_tokens: 0,
    };
    expect(await writes("5m", split)).toBe(900);
  });

  test("5m writes on a 1h pin make the count unknown, never mispriced", async () => {
    const split = {
      ephemeral_5m_input_tokens: 148,
      ephemeral_1h_input_tokens: 100,
    };
    expect(await writes("1h", split)).toBeNull();
  });

  test("1h writes on a 5m pin are unknown too", async () => {
    const split = {
      ephemeral_5m_input_tokens: 0,
      ephemeral_1h_input_tokens: 50,
    };
    expect(await writes("5m", split)).toBeNull();
  });
});
