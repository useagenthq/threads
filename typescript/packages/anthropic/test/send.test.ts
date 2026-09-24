import { describe, expect, test } from "bun:test";
import {
  drain,
  failure,
  recordingFetch,
  renderBody,
  sse,
} from "@threads/adapter-testkit";
import { type Json, memoryContext } from "@threads/core/adapter";
import { anthropic } from "../src";

// SDK-level fetch mocks: the real SDK, no network.

const options = {
  maxTokens: 1024,
  apiKey: "test-key",
};
const head = {
  adapter: { name: "anthropic", version: "1", settings: {} },
  model: { provider: "anthropic", name: "claude-sonnet-5" },
  params: { max_tokens: 1024 },
  system: "",
  tools: [],
};
const request = {
  request_id: "b:e1",
  body: renderBody([
    head,
    { role: "user", content: [{ type: "text", text: "hi" }] },
  ]),
};
const ev = (data: { readonly type: string } & { [key: string]: Json }) => ({
  event: data.type,
  data,
});
const start = ev({
  type: "message_start",
  message: {
    id: "msg_1",
    usage: {
      input_tokens: 12,
      output_tokens: 1,
      cache_read_input_tokens: 100,
      cache_creation_input_tokens: 0,
    },
  },
});
const stop = ev({ type: "message_stop" });

async function run(responses: Response[], live = () => true) {
  const { fetch, calls } = recordingFetch(responses);
  const context = memoryContext(live);
  const out = await drain(
    anthropic("claude-sonnet-5", { ...options, fetch }).send(request, context),
  );
  return { ...out, calls, context };
}

describe("streaming", () => {
  test("text, signed thinking and a tool call become ordered parts; usage maps", async () => {
    const thinking = { type: "thinking", thinking: "", signature: "" };
    const { chunks, calls, context } = await run([
      sse([
        start,
        ev({ type: "content_block_start", index: 0, content_block: thinking }),
        ev({
          type: "content_block_delta",
          index: 0,
          delta: { type: "thinking_delta", thinking: "Look first." },
        }),
        ev({
          type: "content_block_delta",
          index: 0,
          delta: { type: "signature_delta", signature: "sig_1" },
        }),
        ev({ type: "content_block_stop", index: 0 }),
        ev({
          type: "content_block_start",
          index: 1,
          content_block: { type: "text", text: "" },
        }),
        ev({
          type: "content_block_delta",
          index: 1,
          delta: { type: "text_delta", text: "On it." },
        }),
        ev({ type: "content_block_stop", index: 1 }),
        ev({
          type: "content_block_start",
          index: 2,
          content_block: {
            type: "tool_use",
            id: "toolu_1",
            name: "ls",
            input: {},
          },
        }),
        ev({
          type: "content_block_delta",
          index: 2,
          delta: { type: "input_json_delta", partial_json: '{"path":' },
        }),
        ev({
          type: "content_block_delta",
          index: 2,
          delta: { type: "input_json_delta", partial_json: '"."}' },
        }),
        ev({ type: "content_block_stop", index: 2 }),
        ev({
          type: "message_delta",
          delta: { stop_reason: "tool_use" },
          usage: {
            output_tokens: 40,
            output_tokens_details: { thinking_tokens: 9 },
          },
        }),
        stop,
      ]),
    ]);
    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toBe("https://api.anthropic.com/v1/messages");
    const [reasoning, ...rest] = chunks;
    expect(reasoning?.kind === "part" && reasoning.part.type).toBe("reasoning");
    if (reasoning?.kind !== "part" || reasoning.part.type !== "reasoning")
      throw new Error("expected a reasoning part");
    const stored = await context.read(reasoning.part.ref);
    expect(stored.ok && new TextDecoder().decode(stored.value)).toBe(
      '{"type":"thinking","thinking":"Look first.","signature":"sig_1"}',
    );
    expect(reasoning.part.summary).toBe("Look first.");
    expect<unknown>(rest).toEqual([
      { kind: "delta", text: "On it." },
      { kind: "part", part: { type: "text", text: "On it." } },
      {
        kind: "part",
        part: {
          type: "tool_use",
          call_id: "toolu_1",
          name: "ls",
          input: { path: "." },
        },
      },
      {
        kind: "done",
        stop_reason: "tool_use",
        usage: {
          input_tokens: 12,
          output_tokens: 40,
          cache_read_tokens: 100,
          cache_write_tokens: 0,
          reasoning_tokens: 9,
        },
      },
    ]);
  });

  test("usage the provider never reported is null, never 0", async () => {
    const { chunks } = await run([
      sse([
        ev({ type: "message_start", message: { usage: { output_tokens: 1 } } }),
        ev({
          type: "message_delta",
          delta: { stop_reason: "pause_turn" },
          usage: {},
        }),
        stop,
      ]),
    ]);
    expect(chunks.at(-1)).toEqual({
      kind: "done",
      stop_reason: "pause_turn",
      usage: {
        input_tokens: null,
        output_tokens: 1,
        cache_read_tokens: null,
        cache_write_tokens: null,
        reasoning_tokens: null,
      },
    });
  });

  test("web search blocks are hosted_tool parts, citations follow their text", async () => {
    const { chunks } = await run([
      sse([
        start,
        ev({
          type: "content_block_start",
          index: 0,
          content_block: {
            type: "server_tool_use",
            id: "srvtoolu_1",
            name: "web_search",
            input: {},
          },
        }),
        ev({ type: "content_block_stop", index: 0 }),
        ev({
          type: "content_block_start",
          index: 1,
          content_block: {
            type: "text",
            text: "Bun 1.3 is out.",
            citations: null,
          },
        }),
        ev({
          type: "content_block_delta",
          index: 1,
          delta: {
            type: "citations_delta",
            citation: {
              type: "web_search_result_location",
              url: "https://bun.sh/blog",
              title: "Bun",
              cited_text: "Bun 1.3",
              encrypted_index: "x",
            },
          },
        }),
        ev({ type: "content_block_stop", index: 1 }),
        stop,
      ]),
    ]);
    const parts = chunks.flatMap((c) => (c.kind === "part" ? [c.part] : []));
    expect(parts.map((p) => p.type)).toEqual([
      "hosted_tool",
      "text",
      "citation",
    ]);
    expect(parts[2]).toEqual({
      type: "citation",
      source_kind: "web",
      source_id: "https://bun.sh/blog",
      title: "Bun",
      cited_text: "Bun 1.3",
    });
  });

  test("a stream that ends without message_stop is broken (thrown), not a response", async () => {
    const { thrown } = await run([sse([start])]);
    expect(String(thrown)).toContain("before message_stop");
  });
});

describe("rejections before content, one transport attempt each", () => {
  const cases: [string, Response, Json][] = [
    [
      "429 with retry-after",
      failure(
        429,
        { type: "error", error: { type: "rate_limit_error", message: "slow" } },
        { "retry-after": "7" },
      ),
      {
        kind: "rejected",
        http_status: 429,
        reason: "rate_limited",
        retry_after_ms: 7000,
      },
    ],
    [
      "529 overloaded",
      failure(529, {
        type: "error",
        error: { type: "overloaded_error", message: "busy" },
      }),
      { kind: "rejected", http_status: 529, reason: "overloaded" },
    ],
    [
      "500",
      failure(500, {
        type: "error",
        error: { type: "api_error", message: "oops" },
      }),
      { kind: "rejected", http_status: 500, reason: "server_error" },
    ],
    [
      "400 prompt too long",
      failure(400, {
        type: "error",
        error: {
          type: "invalid_request_error",
          message: "prompt is too long: 210000 tokens > 200000 maximum",
        },
      }),
      { kind: "rejected", http_status: 400, reason: "prompt_too_long" },
    ],
    [
      "401",
      failure(401, {
        type: "error",
        error: { type: "authentication_error", message: "no" },
      }),
      { kind: "rejected", http_status: 401, reason: "provider_error" },
    ],
    [
      "an overloaded error event before any content",
      sse([
        start,
        {
          event: "error",
          data: {
            type: "error",
            error: { type: "overloaded_error", message: "busy" },
          },
        },
      ]),
      { kind: "rejected", reason: "overloaded" },
    ],
  ];
  for (const [name, response, expected] of cases)
    test(name, async () => {
      const { chunks, calls } = await run([response]);
      expect<unknown>(chunks).toEqual([expected]);
      expect(calls).toHaveLength(1);
    });

  test("an error event after content is a broken stream (thrown)", async () => {
    const { chunks, thrown } = await run([
      sse([
        start,
        ev({
          type: "content_block_start",
          index: 0,
          content_block: { type: "text", text: "" },
        }),
        ev({
          type: "content_block_delta",
          index: 0,
          delta: { type: "text_delta", text: "Hi" },
        }),
        {
          event: "error",
          data: {
            type: "error",
            error: { type: "overloaded_error", message: "busy" },
          },
        },
      ]),
    ]);
    expect(chunks).toEqual([{ kind: "delta", text: "Hi" }]);
    expect(thrown).toBeDefined();
  });
});

describe("fencing at the transport boundary", () => {
  test("a stale writer's send never leaves: fetch is not called", async () => {
    const { chunks, calls, thrown } = await run([], () => false);
    expect(calls).toHaveLength(0);
    expect(chunks).toEqual([{ kind: "rejected", reason: "stale_epoch" }]);
    expect(thrown).toBeUndefined();
  });

  test("a send queued in the SDK that loses the lease before fetch sends nothing", async () => {
    let lost = false;
    const { fetch, calls } = recordingFetch([sse([start, stop])]);
    const stream = anthropic("claude-sonnet-5", { ...options, fetch }).send(
      request,
      memoryContext(() => !lost),
    );
    const pending = drain(stream);
    lost = true; // the lease is lost while the SDK is still preparing the request
    const { chunks, thrown } = await pending;
    expect(calls).toHaveLength(0);
    expect(thrown).toBeUndefined();
    expect(chunks).toEqual([{ kind: "rejected", reason: "stale_epoch" }]);
  });
});
