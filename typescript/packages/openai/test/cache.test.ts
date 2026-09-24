import { describe, expect, test } from "bun:test";
import { openai } from "../src";

// OpenAI caches prompts automatically (lane 10): info.cache declares 24 hours with extended
// retention, else the documented in-memory lower bound of 5 minutes.

const key = { apiKey: "test-key" } as const;

describe("openai declares its cache lifetime", () => {
  test("in-memory retention: 5 minutes", () => {
    expect(openai("gpt-5.5", key).info.cache).toEqual({ ttl_ms: 300_000 });
  });

  test('prompt_cache_retention "24h": 24 hours', () => {
    const params = { prompt_cache_retention: "24h" };
    expect(openai("gpt-5.5", { ...key, params }).info.cache).toEqual({
      ttl_ms: 86_400_000,
    });
  });
});
