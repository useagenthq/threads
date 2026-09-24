import { describe, expect, test } from "bun:test";
import { type AiSdkOptions, aiSdk } from "../src";
import { fakeModel, offline } from "./fake";

// threads can't see the provider behind an AI SDK model, so its cache lifetime is unknown unless
// the caller declares it (lane 10); an agent using it then needs context.cache_ttl_ms.

const base = {
  model: fakeModel([]).factory,
  fetch: offline,
  maxInputTokens: 128_000,
  maxOutputTokens: 8192,
};
const make = (more: Partial<AiSdkOptions> = {}) => aiSdk({ ...base, ...more });

describe("aiSdk cacheTtlMs", () => {
  test("absent: the lifetime is unknown", () => {
    expect(make().info.cache).toBeUndefined();
  });

  test("a number declares the lifetime, and none declares no caching", () => {
    expect(make({ cacheTtlMs: 300_000 }).info.cache).toEqual({
      ttl_ms: 300_000,
    });
    expect(make({ cacheTtlMs: "none" }).info.cache).toBe("none");
  });

  test.each([0, -1, 1.5])("%p is refused", (cacheTtlMs) => {
    expect(() => make({ cacheTtlMs })).toThrow(
      expect.objectContaining({ code: "invalid_config" }),
    );
  });

  test("a string other than none is refused", () => {
    // @ts-expect-error: an untyped caller passes a duration string
    expect(() => make({ cacheTtlMs: "5m" })).toThrow(
      expect.objectContaining({ code: "invalid_config" }),
    );
  });
});
