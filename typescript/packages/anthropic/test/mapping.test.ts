import { describe, expect, test } from "bun:test";
import {
  drain,
  recordingFetch,
  renderBody,
  renderCase,
} from "@threads/adapter-testkit";
import { memoryContext, parseRender } from "@threads/core/adapter";
import { anthropic } from "../src";
import { toAnthropic } from "../src/request";

const model = anthropic("claude-sonnet-5", {
  maxTokens: 1024,
  params: { temperature: 0 },
});
const head = {
  adapter: model.info.adapter,
  model: model.info.model,
  params: model.info.params,
  system: "You are a helpful agent.",
  tools: [
    {
      name: "read_file",
      description: "Read a file.",
      input_schema: { type: "object" },
    },
  ],
};
const encoder = new TextEncoder();

describe("golden: Render v1 conformance cases → Messages API request", () => {
  for (const name of [
    "render-user-image-input",
    "render-screenshot-tool-result",
    "render-deferred-tool-loaded",
    "render-tool-results-cleared",
    "render-thinking-block-replay",
    "render-hosted-search-citations",
  ])
    test(name, async () => {
      const { body, context } = await renderCase(name, model.info);
      expect(await toAnthropic(parseRender(body), context)).toMatchSnapshot();
    });
});

describe("replay", () => {
  test("a recorded thinking block goes back byte for byte, by ref", async () => {
    const context = memoryContext();
    const block = {
      type: "thinking",
      thinking: "Read it first.",
      signature: "sig_abc",
    };
    const ref = await context.put(
      encoder.encode(JSON.stringify(block)),
      "application/json",
    );
    const body = renderBody([
      head,
      { role: "user", content: [{ type: "text", text: "hi" }] },
      {
        role: "assistant",
        content: [
          {
            type: "reasoning",
            provider: "anthropic",
            model: "claude-sonnet-5",
            format: "thinking",
            ref,
          },
          {
            type: "tool_use",
            call_id: "toolu_1",
            name: "read_file",
            input: {},
          },
        ],
      },
      {
        role: "tool",
        call_id: "toolu_1",
        is_error: false,
        content: [{ type: "text", text: "ok" }],
      },
    ]);
    const mapped = await toAnthropic(parseRender(body), context);
    expect(mapped.ok && mapped.body["messages"]).toEqual([
      { role: "user", content: [{ type: "text", text: "hi" }] },
      {
        role: "assistant",
        content: [
          block,
          { type: "tool_use", id: "toolu_1", name: "read_file", input: {} },
        ],
      },
      {
        role: "user",
        content: [
          {
            type: "tool_result",
            tool_use_id: "toolu_1",
            is_error: false,
            content: [{ type: "text", text: "ok" }],
          },
        ],
      },
    ]);
  });

  test("another provider's reasoning is refused before dispatch, never dropped", async () => {
    const context = memoryContext();
    const ref = await context.put(encoder.encode("{}"), "application/json");
    const { fetch, calls } = recordingFetch([]);
    const m = anthropic("claude-sonnet-5", { ...options(), fetch });
    const body = renderBody([
      head,
      {
        role: "assistant",
        content: [
          {
            type: "reasoning",
            provider: "openai",
            model: "o",
            format: "openai_reasoning",
            ref,
          },
        ],
      },
    ]);
    const out = await drain(m.send({ request_id: "b:e", body }, context));
    expect(out.chunks).toEqual([
      { kind: "rejected", reason: "continuation_unsupported" },
    ]);
    expect(calls).toHaveLength(0);
  });

  test("audio is content_unsupported: refused, nothing sent", async () => {
    const context = memoryContext();
    const ref = await context.put(new Uint8Array([1]), "audio/wav");
    const body = renderBody([
      head,
      {
        role: "user",
        content: [
          {
            type: "audio_ref",
            ref: { ...ref, media_type: "audio/wav" },
            duration_ms: 10,
          },
        ],
      },
    ]);
    const mapped = await toAnthropic(parseRender(body), context);
    expect(mapped).toEqual({
      ok: false,
      code: "content_unsupported",
      message: "anthropic takes no audio input",
    });
  });
});

describe("setup", () => {
  test("params can't override what the render decides", () => {
    expect(() =>
      anthropic("claude-sonnet-5", { ...options(), params: { messages: [] } }),
    ).toThrow("can't set messages");
  });

  test("model info is declared, lookup honestly none", () => {
    expect(model.info.lookup).toBe("none");
    expect(model.lookup).toBeUndefined();
    expect(model.info.params).toEqual({ temperature: 0, max_tokens: 1024 });
    expect(model.info.accepts).toEqual(["text", "image_ref", "document_ref"]);
  });
});

function options() {
  return {
    maxTokens: 1024,
  };
}
