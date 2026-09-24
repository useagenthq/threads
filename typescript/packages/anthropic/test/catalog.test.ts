import { describe, expect, test } from "bun:test";
import {
  drain,
  recordingFetch,
  renderBody,
  sse,
} from "@threads/adapter-testkit";
import { memoryContext } from "@threads/core/adapter";
import { z } from "zod";
import { anthropic } from "../src";

// Lane 06: anthropic("<id>") takes its limits from spec/models/anthropic.v1.json, pins the
// per-request cap as params.max_tokens, and sends exactly that cap.

describe("anthropic(id)", () => {
  test("a listed id needs no options: verified limits and the default cap", () => {
    const { info } = anthropic("claude-sonnet-5");
    expect(info.model).toEqual({
      provider: "anthropic",
      name: "claude-sonnet-5",
    });
    expect(info.params).toEqual({ max_tokens: 8192 });
    expect(info.limits).toEqual({
      provider: "anthropic",
      name: "claude-sonnet-5",
      context_window: 1_000_000,
      max_output_tokens: 128_000,
      input_billing_bound: "context_window",
    });
    expect(anthropic("claude-haiku-4-5-20251001").info.limits).toMatchObject({
      context_window: 200_000,
      max_output_tokens: 64_000,
    });
  });

  test("each option overrides one limit", () => {
    const { info } = anthropic("claude-sonnet-5", {
      maxInputTokens: 200_000,
      maxTokens: 32_000,
    });
    expect(info.params).toEqual({ max_tokens: 32_000 });
    expect(info.limits).toMatchObject({
      context_window: 200_000,
      max_output_tokens: 128_000,
    });
  });

  test("an unlisted id needs both limits, and says so", () => {
    expect(() => anthropic("claude-next")).toThrow(
      'anthropic: unknown model "claude-next"; pass maxInputTokens and maxOutputTokens',
    );
    // A near-miss id sees the ids it may have meant; matching stays exact.
    expect(() => anthropic("claude-haiku-4-5")).toThrow(
      "or use a listed id: claude-fable-5-1, claude-haiku-4-5-20251001, claude-opus-5-5, claude-sonnet-5",
    );
    const { info } = anthropic("claude-next", {
      maxInputTokens: 500_000,
      maxOutputTokens: 4096,
    });
    expect(info.params).toEqual({ max_tokens: 4096 });
  });

  test("the cap is set by maxTokens only, never by params", () => {
    expect(() =>
      anthropic("claude-sonnet-5", { params: { max_tokens: 10 } }),
    ).toThrow("anthropic params can't set max_tokens: pass maxTokens");
    expect(() => anthropic("claude-sonnet-5", { maxTokens: 200_000 })).toThrow(
      expect.objectContaining({ code: "invalid_config" }),
    );
  });

  test("the wire body's max_tokens is the pinned params.max_tokens", async () => {
    const { pinned, body } = await sendOnce({ maxTokens: 2048 });
    expect(pinned).toEqual({ max_tokens: 2048 });
    expect(body.max_tokens).toBe(2048);
  });
});

const Body = z.looseObject({ max_tokens: z.number() });

/** One send through the real SDK with a recorded transport: the pinned params and the body. */
async function sendOnce(options: { readonly maxTokens: number }) {
  const { fetch, calls } = recordingFetch([
    sse([{ event: "message_stop", data: { type: "message_stop" } }]),
  ]);
  const model = anthropic("claude-sonnet-5", {
    ...options,
    apiKey: "test-key",
    fetch,
  });
  const { adapter, params } = model.info;
  const body = renderBody([
    { adapter, model: model.info.model, params, system: "", tools: [] },
    { role: "user", content: [{ type: "text", text: "hi" }] },
  ]);
  await drain(model.send({ request_id: "b:e1", body }, memoryContext()));
  return { pinned: params, body: Body.parse(calls[0]?.body) };
}
