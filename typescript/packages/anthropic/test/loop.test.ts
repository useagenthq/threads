import { describe, expect, test } from "bun:test";
import { recordingFetch, sse } from "@threads/adapter-testkit";
import { agent, sqlite } from "@threads/core";
import { type Json, markTestKit } from "@threads/core/adapter";
import { z } from "zod";
import { anthropic } from "../src";

// The adapter inside the real loop, with a mocked transport: each stop reason ends the run the
// way spec/schema/README.md "Turn endings by stop_reason" says.

const ev = (data: { readonly type: string } & { [key: string]: Json }) => ({
  event: data.type,
  data,
});

function reply(text: string, stop: string): Response {
  return sse([
    ev({ type: "message_start", message: { usage: { input_tokens: 9 } } }),
    ev({
      type: "content_block_start",
      index: 0,
      content_block: { type: "text", text: "" },
    }),
    ev({
      type: "content_block_delta",
      index: 0,
      delta: { type: "text_delta", text },
    }),
    ev({ type: "content_block_stop", index: 0 }),
    ev({
      type: "message_delta",
      delta: { stop_reason: stop },
      usage: { output_tokens: 3 },
    }),
    ev({ type: "message_stop" }),
  ]);
}

async function run(responses: Response[]) {
  const { fetch, calls } = recordingFetch(responses);
  const model = anthropic({
    model: "claude-sonnet-5",
    maxTokens: 256,
    contextWindow: 200_000,
    maxOutputTokens: 64_000,
    apiKey: "test-key",
    fetch,
  });
  markTestKit(model);
  const bot = agent({ instructions: "Answer briefly.", model });
  const result = await bot.run("Search for it.", {
    store: sqlite(":memory:"),
  });
  return { result, calls };
}

describe("stop reasons through the loop", () => {
  test("pause_turn asks again with the paused content as-is, then completes", async () => {
    const { result, calls } = await run([
      reply("Searching.", "pause_turn"),
      reply("Found it.", "end_turn"),
    ]);
    expect(result.status).toBe("completed");
    expect(result.status === "completed" && result.output).toBe("Found it.");
    expect(calls).toHaveLength(2);
    const { messages } = z
      .object({ messages: z.array(z.unknown()) })
      .parse(calls[1]?.body);
    expect(messages.at(-1)).toEqual({
      role: "assistant",
      content: [{ type: "text", text: "Searching." }],
    });
  });

  test("model_context_window_exceeded fails the run context_exhausted", async () => {
    const { result } = await run([
      reply("Half an ans", "model_context_window_exceeded"),
    ]);
    expect(result.status === "failed" && result.error.code).toBe(
      "context_exhausted",
    );
  });

  test("a stop reason the adapter can't name fails the run, never completes it", async () => {
    const { result } = await run([reply("Hmm", "some_new_reason")]);
    expect(result.status === "failed" && result.error.code).toBe("model_error");
  });
});
