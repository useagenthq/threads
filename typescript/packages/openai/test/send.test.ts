import { describe, expect, test } from "bun:test";
import {
  deltaProblems,
  drain,
  failure,
  recordingFetch,
  renderBody,
  sse,
} from "@threads/adapter-testkit";
import { type Json, memoryContext } from "@threads/core/adapter";
import { openai } from "../src";

// SDK-level fetch mocks: the real SDK, no network.

const options = {
  apiKey: "test-key",
};
const request = {
  request_id: "b:e1",
  body: renderBody([
    {
      adapter: { name: "openai", version: "1", settings: {} },
      model: { provider: "openai", name: "gpt-5.5" },
      params: {},
      system: "",
      tools: [],
    },
    { role: "user", content: [{ type: "text", text: "hi" }] },
  ]),
};
const ev = (data: { readonly type: string } & { [key: string]: Json }) => ({
  event: data.type,
  data,
});
const usage = {
  input_tokens: 120,
  input_tokens_details: { cached_tokens: 100, cache_write_tokens: 5 },
  output_tokens: 30,
  output_tokens_details: { reasoning_tokens: 12 },
  total_tokens: 150,
};
const completed = (u: Json = usage) =>
  ev({
    type: "response.completed",
    response: { status: "completed", usage: u },
  });

async function run(responses: Response[], live = () => true) {
  const { fetch, calls } = recordingFetch(responses);
  const context = memoryContext(live);
  const out = await drain(
    openai("gpt-5.5", { ...options, fetch }).send(request, context),
  );
  return { ...out, calls, context };
}

describe("streaming", () => {
  test("reasoning, text with a citation, and a function call; usage excludes cache", async () => {
    const reasoning = {
      id: "rs_1",
      type: "reasoning",
      summary: [{ type: "summary_text", text: "Plan." }],
      encrypted_content: "gAAA",
    };
    const { chunks, calls, context } = await run([
      sse([
        ev({ type: "response.created", response: { id: "resp_1" } }),
        ev({
          type: "response.output_item.done",
          output_index: 0,
          item: reasoning,
        }),
        ev({
          type: "response.output_text.delta",
          content_index: 0,
          delta: "See docs.",
        }),
        ev({
          type: "response.output_item.done",
          output_index: 1,
          item: {
            id: "msg_1",
            type: "message",
            role: "assistant",
            status: "completed",
            content: [
              {
                type: "output_text",
                text: "See docs.",
                annotations: [
                  {
                    type: "url_citation",
                    url: "https://x.dev",
                    title: "X",
                    start_index: 0,
                    end_index: 3,
                  },
                ],
              },
            ],
          },
        }),
        ev({
          type: "response.output_item.done",
          output_index: 2,
          item: {
            type: "function_call",
            id: "fc_1",
            call_id: "call_1",
            name: "ls",
            arguments: '{"path":"."}',
          },
        }),
        completed(),
      ]),
    ]);
    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toBe("https://api.openai.com/v1/responses");
    expect(deltaProblems(chunks)).toEqual([]);
    const [first, ...rest] = chunks;
    if (first?.kind !== "part" || first.part.type !== "reasoning")
      throw new Error("expected a reasoning part first");
    expect(first.part.summary).toBe("Plan.");
    const stored = await context.read(first.part.ref);
    expect(
      stored.ok && JSON.parse(new TextDecoder().decode(stored.value)),
    ).toEqual(reasoning);
    expect<unknown>(rest).toEqual([
      { kind: "delta", part: 1, text: "See docs." },
      { kind: "part", part: { type: "text", text: "See docs." } },
      {
        kind: "part",
        part: {
          type: "citation",
          source_kind: "web",
          source_id: "https://x.dev",
          title: "X",
        },
      },
      {
        kind: "part",
        part: {
          type: "tool_use",
          call_id: "call_1",
          name: "ls",
          input: { path: "." },
        },
      },
      {
        kind: "done",
        stop_reason: "tool_use",
        usage: {
          input_tokens: 15,
          output_tokens: 30,
          cache_read_tokens: 100,
          cache_write_tokens: 5,
          reasoning_tokens: 12,
        },
      },
    ]);
  });

  test("a hosted web search item is kept whole as hosted_tool", async () => {
    const { chunks } = await run([
      sse([
        ev({
          type: "response.output_item.done",
          item: { type: "web_search_call", id: "ws_1", status: "completed" },
        }),
        completed(),
      ]),
    ]);
    const part = chunks[0]?.kind === "part" ? chunks[0].part : undefined;
    expect(part?.type === "hosted_tool" && part.name).toBe("web_search_call");
  });

  test("missing usage is null, never 0; max_output_tokens stops as max_tokens", async () => {
    const { chunks } = await run([
      sse([
        ev({
          type: "response.incomplete",
          response: { incomplete_details: { reason: "max_output_tokens" } },
        }),
      ]),
    ]);
    expect(chunks).toEqual([
      {
        kind: "done",
        stop_reason: "max_tokens",
        usage: {
          input_tokens: null,
          output_tokens: null,
          cache_read_tokens: null,
          cache_write_tokens: null,
          reasoning_tokens: null,
        },
      },
    ]);
  });

  test("a refusal is text plus stop_reason refusal", async () => {
    const { chunks } = await run([
      sse([
        ev({
          type: "response.output_item.done",
          item: {
            type: "message",
            content: [{ type: "refusal", refusal: "No." }],
          },
        }),
        completed(),
      ]),
    ]);
    expect(
      chunks.map((c) => (c.kind === "done" ? c.stop_reason : c.kind)),
    ).toEqual(["part", "refusal"]);
  });
});

describe("rejections before content, one transport attempt each", () => {
  const err = (code: string) => ({ error: { message: code, type: "x", code } });
  const cases: [string, Response, Json][] = [
    [
      "429 with retry-after-ms",
      failure(429, err("rate_limit_exceeded"), { "retry-after-ms": "250" }),
      {
        kind: "rejected",
        http_status: 429,
        reason: "rate_limited",
        retry_after_ms: 250,
      },
    ],
    [
      "429 insufficient_quota won't clear: provider_error",
      failure(429, err("insufficient_quota")),
      { kind: "rejected", http_status: 429, reason: "provider_error" },
    ],
    [
      "503",
      failure(503, err("overloaded")),
      { kind: "rejected", http_status: 503, reason: "overloaded" },
    ],
    [
      "400 context_length_exceeded",
      failure(400, err("context_length_exceeded")),
      { kind: "rejected", http_status: 400, reason: "prompt_too_long" },
    ],
    [
      "response.failed before content",
      sse([
        ev({
          type: "response.failed",
          response: { error: { code: "server_error", message: "oops" } },
        }),
      ]),
      { kind: "rejected", reason: "server_error" },
    ],
  ];
  for (const [name, response, expected] of cases)
    test(name, async () => {
      const { chunks, calls } = await run([response]);
      expect<unknown>(chunks).toEqual([expected]);
      expect(calls).toHaveLength(1);
    });

  test("a failure after content is a broken stream (thrown)", async () => {
    const { chunks, thrown } = await run([
      sse([
        ev({
          type: "response.output_text.delta",
          content_index: 0,
          delta: "Hi",
        }),
        ev({
          type: "response.failed",
          response: { error: { code: "server_error", message: "oops" } },
        }),
      ]),
    ]);
    expect(chunks).toEqual([{ kind: "delta", part: 0, text: "Hi" }]);
    expect(thrown).toBeDefined();
  });
});

describe("fencing at the transport boundary", () => {
  test("a send queued in the SDK that loses the lease before fetch sends nothing", async () => {
    let lost = false;
    const { fetch, calls } = recordingFetch([sse([completed()])]);
    const pending = drain(
      openai("gpt-5.5", { ...options, fetch }).send(
        request,
        memoryContext(() => !lost),
      ),
    );
    lost = true; // the lease is lost while the SDK is still preparing the request
    const { chunks, thrown } = await pending;
    expect(calls).toHaveLength(0);
    expect(thrown).toBeUndefined();
    expect(chunks).toEqual([{ kind: "rejected", reason: "stale_epoch" }]);
  });
});
