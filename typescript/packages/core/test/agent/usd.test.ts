import { describe, expect, test } from "bun:test";
import { ConfigError, usd } from "../../src";

describe("usd()", () => {
  test("dollars as integer nano-dollars, rounded half up", () => {
    expect(usd(0.5)).toBe(500_000_000);
    expect(usd(0)).toBe(0);
    expect(usd(1.25)).toBe(1_250_000_000);
    expect(usd(0.0000000005)).toBe(1);
  });

  test("a negative, non-finite or too large amount is a ConfigError", () => {
    for (const bad of [-1, Number.NaN, Number.POSITIVE_INFINITY, 1e7])
      expect(() => usd(bad)).toThrow(ConfigError);
  });
});
