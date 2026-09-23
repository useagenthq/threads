import { describe, expect, test } from "bun:test";
import { reservation, tokenBounds } from "../../src/reduce/cost";

// An attempt's bounds (spec/schema/README.md, Budget enforcement): only a JSON integer max_tokens
// bounds the output. A fraction or a boolean is not one; Python agrees (bool is not read as 1).

const MODEL = {
  provider: "scripted",
  name: "scripted-1",
  context_window: 200_000,
  max_output_tokens: 8192,
  input_billing_bound: "context_window",
} as const;
const PRICE = { input: 3000, output: 15_000 };

describe("an attempt's bounds", () => {
  test.each([true, false, 1.5, "1024", null])(
    "max_tokens %p bounds nothing",
    (maxTokens) => {
      const params = { max_tokens: maxTokens };
      expect(tokenBounds(MODEL, params, undefined)).toEqual({
        input: 200_000,
      });
      expect(reservation(MODEL, PRICE, params, undefined)).toBeUndefined();
    },
  );

  test("an integer max_tokens bounds the output and the cost", () => {
    const params = { max_tokens: 1024 };
    expect(tokenBounds(MODEL, params, undefined)).toEqual({
      input: 200_000,
      output: 1024,
    });
    expect(reservation(MODEL, PRICE, params, undefined)).toBe(
      200_000 * 3000 + 1024 * 15_000,
    );
  });
});
