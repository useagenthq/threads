import { describe, expect, test } from "bun:test";
import { renderBody, renderCase } from "@threads/adapter-testkit";
import { memoryContext, parseRender } from "@threads/core/adapter";
import { openai } from "../src";
import { toOpenAI } from "../src/request";

const options = {
  model: "gpt-5.5",
  contextWindow: 400_000,
  maxOutputTokens: 128_000,
};
const model = openai({
  ...options,
  params: { max_output_tokens: 1024, reasoning: { effort: "low" } },
});
const encoder = new TextEncoder();

describe("golden: Render v1 conformance cases → Responses API request", () => {
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
      expect(await toOpenAI(parseRender(body), context)).toMatchSnapshot();
    });
});

describe("replay", () => {
  test("a recorded reasoning item and function call go back as recorded", async () => {
    const context = memoryContext();
    const item = {
      id: "rs_1",
      type: "reasoning",
      summary: [],
      encrypted_content: "gAAA",
    };
    const ref = await context.put(
      encoder.encode(JSON.stringify(item)),
      "application/json",
    );
    const body = renderBody([
      {
        adapter: model.info.adapter,
        model: model.info.model,
        params: model.info.params,
        system: "Be brief.",
        tools: [],
      },
      { role: "user", content: [{ type: "text", text: "ls" }] },
      {
        role: "assistant",
        content: [
          {
            type: "reasoning",
            provider: "openai",
            model: "gpt-5.5",
            format: "openai_reasoning",
            ref,
          },
          { type: "tool_use", call_id: "call_1", name: "ls", input: { a: 1 } },
        ],
      },
      {
        role: "tool",
        call_id: "call_1",
        is_error: true,
        content: [{ type: "text", text: "denied" }],
      },
    ]);
    const mapped = await toOpenAI(parseRender(body), context);
    expect(mapped.ok && mapped.body["input"]).toEqual([
      { role: "user", content: [{ type: "input_text", text: "ls" }] },
      item,
      {
        type: "function_call",
        call_id: "call_1",
        name: "ls",
        arguments: '{"a":1}',
      },
      {
        type: "function_call_output",
        call_id: "call_1",
        output: [
          { type: "input_text", text: "[tool error]" },
          { type: "input_text", text: "denied" },
        ],
      },
    ]);
    expect(mapped.ok && mapped.body["include"]).toEqual([
      "reasoning.encrypted_content",
    ]);
    expect(mapped.ok && mapped.body["store"]).toBe(false);
  });
});

describe("setup", () => {
  test("params can't override what the render decides", () => {
    expect(() => openai({ ...options, params: { store: true } })).toThrow(
      "can't set store",
    );
  });

  test("lookup is honestly none: store is false", () => {
    expect(model.info.lookup).toBe("none");
    expect(model.lookup).toBeUndefined();
  });
});
