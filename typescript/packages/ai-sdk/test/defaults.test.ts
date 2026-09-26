import { describe, expect, test } from "bun:test";
import { type AiSdkOptions, aiSdk } from "../src";
import { fakeModel, offline } from "./fake";

// The defaults spec/api.json declares for aiSdk. There is no catalog behind an AI SDK model id,
// so both limits are required and everything else the provider would tell us has to be assumed
// conservatively: the shared output cap, text-only input, and no input billing bound.

const base = {
  model: fakeModel([]).factory,
  fetch: offline,
  maxInputTokens: 128_000,
  maxOutputTokens: 32_000,
};
const make = (more: Partial<AiSdkOptions> = {}) => aiSdk({ ...base, ...more });

describe("aiSdk defaults", () => {
  test("maxTokens absent: min(8192, maxOutputTokens), pinned as params.max_tokens", () => {
    expect(make().info.params).toEqual({ max_tokens: 8192 });
    expect(make({ maxOutputTokens: 4096 }).info.params).toEqual({
      max_tokens: 4096,
    });
  });

  test("accepts absent: text only", () => {
    expect(make().info.accepts).toEqual(["text"]);
  });

  test("inputBillingBound absent: none", () => {
    expect(make().info.limits.input_billing_bound).toBe("none");
  });

  test("both limits are pinned as given", () => {
    expect(make().info.limits.context_window).toBe(128_000);
    expect(make().info.limits.max_output_tokens).toBe(32_000);
  });
});
