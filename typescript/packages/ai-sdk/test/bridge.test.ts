import { describe, expect, test } from "bun:test";
import { APICallError, type LanguageModelV4Usage } from "@ai-sdk/provider";
import {
  drain,
  recordingFetch,
  renderBody,
  renderCase,
  sse,
} from "@threads/adapter-testkit";
import { type Json, memoryContext, parseRender } from "@threads/core/adapter";
import { aiSdk, threadsFetch } from "../src";
import { toPrompt } from "../src/prompt";
import { type Entry, fakeModel, unknownUsage, usage } from "./fake";

const limits = { contextWindow: 128_000, maxOutputTokens: 8192 };

function bridge(script: readonly Entry[], live = () => true) {
  const { model, calls } = fakeModel(script);
  const m = aiSdk({ model, ...limits, params: { maxOutputTokens: 512 } });
  const head = {
    adapter: m.info.adapter,
    model: m.info.model,
    params: m.info.params,
    system: "Be brief.",
    tools: [],
  };
  const body = (lines: Json[]) => renderBody([head, ...lines]);
  const send = (...lines: Json[]) =>
    drain(m.send({ request_id: "b:e1", body: body(lines) }, context));
  const context = memoryContext(live);
  return { m, calls, send, context, head };
}

const hi = { role: "user", content: [{ type: "text", text: "hi" }] };
const finish = (
  unified: "stop" | "tool-calls" | "length" | "other",
  u: LanguageModelV4Usage = usage,
) => ({
  type: "finish" as const,
  finishReason: { unified, raw: undefined },
  usage: u,
});

describe("golden: Render v1 conformance cases → AI SDK prompt", () => {
  const { model } = fakeModel([]);
  const m = aiSdk({ model, ...limits, accepts: ["text", "image_ref"] });
  for (const name of [
    "render-user-image-input",
    "render-screenshot-tool-result",
    "render-deferred-tool-loaded",
    "render-thinking-block-replay",
  ])
    test(name, async () => {
      const { body, context } = await renderCase(name, m.info);
      expect(
        await toPrompt(parseRender(body), context, m.info.accepts),
      ).toMatchSnapshot();
    });
});

describe("streaming", () => {
  test("text, signed reasoning, a tool call and a source; usage excludes cache", async () => {
    const signed = { anthropic: { signature: "sig_1" } };
    const { send, calls, context } = bridge([
      [
        { type: "stream-start", warnings: [] },
        { type: "reasoning-start", id: "r" },
        { type: "reasoning-delta", id: "r", delta: "Plan." },
        { type: "reasoning-end", id: "r", providerMetadata: signed },
        { type: "text-start", id: "t" },
        { type: "text-delta", id: "t", delta: "On " },
        { type: "text-delta", id: "t", delta: "it." },
        { type: "text-end", id: "t" },
        {
          type: "source",
          sourceType: "url",
          id: "s",
          url: "https://x.dev",
          title: "X",
        },
        {
          type: "tool-call",
          toolCallId: "call_1",
          toolName: "ls",
          input: '{"path":"."}',
        },
        finish("tool-calls"),
      ],
    ]);
    const { chunks } = await send(hi);
    expect(calls).toHaveLength(1);
    expect(calls[0]?.maxOutputTokens).toBe(512);
    expect(calls[0]?.prompt).toEqual([
      { role: "system", content: "Be brief." },
      { role: "user", content: [{ type: "text", text: "hi" }] },
    ]);
    const [first, ...rest] = chunks;
    if (first?.kind !== "part" || first.part.type !== "reasoning")
      throw new Error("expected reasoning first");
    expect(first.part.provider).toBe("acme_chat");
    const stored = await context.read(first.part.ref);
    expect(
      stored.ok && JSON.parse(new TextDecoder().decode(stored.value)),
    ).toEqual({
      type: "reasoning",
      text: "Plan.",
      providerOptions: signed,
    });
    expect<unknown>(rest).toEqual([
      { kind: "delta", text: "On " },
      { kind: "delta", text: "it." },
      { kind: "part", part: { type: "text", text: "On it." } },
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
          input_tokens: 20,
          output_tokens: 30,
          cache_read_tokens: 100,
          cache_write_tokens: 0,
          reasoning_tokens: 12,
        },
      },
    ]);
  });

  test("usage the provider didn't report is null, never 0", async () => {
    const { send } = bridge([[finish("stop", unknownUsage)]]);
    expect((await send(hi)).chunks).toEqual([
      {
        kind: "done",
        stop_reason: "end_turn",
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

  test("a provider-executed tool is a hosted_tool, and goes back as recorded", async () => {
    const call = {
      type: "tool-call" as const,
      toolCallId: "ws_1",
      toolName: "web_search",
      input: '{"q":"bun"}',
      providerExecuted: true,
    };
    const { send, context, m, calls } = bridge([
      [call, finish("stop")],
      [finish("stop")],
    ]);
    const { chunks } = await send(hi);
    const hosted = chunks[0]?.kind === "part" ? chunks[0].part : undefined;
    if (hosted?.type !== "hosted_tool") throw new Error("expected hosted_tool");
    expect(hosted.name).toBe("web_search");
    const head = {
      adapter: m.info.adapter,
      model: m.info.model,
      params: m.info.params,
      system: "",
      tools: [],
    };
    await drain(
      m.send(
        {
          request_id: "b:e2",
          body: renderBody([
            head,
            hi,
            { role: "assistant", content: [hosted] },
          ]),
        },
        context,
      ),
    );
    expect(calls[1]?.prompt.at(-1)).toEqual({
      role: "assistant",
      content: [
        {
          type: "tool-call",
          toolCallId: "ws_1",
          toolName: "web_search",
          input: { q: "bun" },
          providerExecuted: true,
        },
      ],
    });
  });

  test("provider metadata on text and tool calls goes back unchanged (thought signatures)", async () => {
    const onText = { google: { thoughtSignature: "sig_text" } };
    const onCall = { google: { thoughtSignature: "sig_call" } };
    const { send, calls, m, context } = bridge([
      [
        { type: "text-start", id: "t", providerMetadata: onText },
        { type: "text-delta", id: "t", delta: "Looking." },
        { type: "text-end", id: "t" },
        {
          type: "tool-call",
          toolCallId: "call_1",
          toolName: "ls",
          input: "{}",
          providerMetadata: onCall,
        },
        finish("tool-calls"),
      ],
      [finish("stop")],
    ]);
    const { chunks } = await send(hi);
    const parts = chunks.flatMap((c) => (c.kind === "part" ? [c.part] : []));
    const head = {
      adapter: m.info.adapter,
      model: m.info.model,
      params: m.info.params,
      system: "",
      tools: [],
    };
    await drain(
      m.send(
        {
          request_id: "b:e2",
          body: renderBody([
            head,
            hi,
            { role: "assistant", content: parts },
            {
              role: "tool",
              call_id: "call_1",
              is_error: false,
              content: [{ type: "text", text: "a b" }],
            },
          ]),
        },
        context,
      ),
    );
    expect(calls[1]?.prompt[1]).toEqual({
      role: "assistant",
      content: [
        { type: "text", text: "Looking.", providerOptions: onText },
        {
          type: "tool-call",
          toolCallId: "call_1",
          toolName: "ls",
          input: {},
          providerOptions: onCall,
        },
      ],
    });
  });

  test("a stream that ends without finish is broken (thrown)", async () => {
    const { send } = bridge([[{ type: "text-start", id: "t" }]]);
    expect(String((await send(hi)).thrown)).toContain("before finish");
  });
});

describe("rejections before content, one attempt each (no hidden retry)", () => {
  const apiError = (statusCode: number, headers: Record<string, string> = {}) =>
    new APICallError({
      message: "failed",
      url: "https://acme.dev",
      requestBodyValues: {},
      statusCode,
      responseHeaders: headers,
      isRetryable: true,
    });

  test("429 thrown by doStream: rate_limited with the provider's wait", async () => {
    const { send, calls } = bridge([
      { throws: apiError(429, { "retry-after": "3" }) },
    ]);
    expect((await send(hi)).chunks).toEqual([
      {
        kind: "rejected",
        http_status: 429,
        reason: "rate_limited",
        retry_after_ms: 3000,
      },
    ]);
    expect(calls).toHaveLength(1);
  });

  test("an error part before content: its class", async () => {
    const { send } = bridge([[{ type: "error", error: apiError(529) }]]);
    expect((await send(hi)).chunks).toEqual([
      { kind: "rejected", http_status: 529, reason: "overloaded" },
    ]);
  });

  test("an error part after content is a broken stream (thrown)", async () => {
    const { send } = bridge([
      [
        { type: "text-start", id: "t" },
        { type: "text-delta", id: "t", delta: "Hi" },
        { type: "error", error: apiError(529) },
      ],
    ]);
    const { chunks, thrown } = await send(hi);
    expect(chunks).toEqual([{ kind: "delta", text: "Hi" }]);
    expect(thrown).toBeDefined();
  });

  test("an error with no HTTP status stays unknown (thrown)", async () => {
    const { send } = bridge([{ throws: new TypeError("socket hang up") }]);
    expect((await send(hi)).thrown).toBeInstanceOf(TypeError);
  });
});

describe("refused before dispatch", () => {
  test("an input part the model doesn't declare is content_unsupported; doStream never runs", async () => {
    const { send, calls, context } = bridge([]);
    const ref = await context.put(new Uint8Array([137, 80]), "image/png");
    const { chunks } = await send({
      role: "user",
      content: [
        {
          type: "image_ref",
          ref: { ...ref, media_type: "image/png" },
          width: 1,
          height: 1,
        },
      ],
    });
    expect(chunks).toEqual([
      { kind: "rejected", reason: "content_unsupported" },
    ]);
    expect(calls).toHaveLength(0);
  });

  test("bad call params fail setup", () => {
    const { model } = fakeModel([]);
    expect(() =>
      aiSdk({ model, ...limits, params: { maxOutputTokens: "lots" } }),
    ).toThrow("aiSdk params");
  });
});

describe("fencing", () => {
  test("a stale writer never reaches doStream", async () => {
    const { send, calls } = bridge([[finish("stop")]], () => false);
    expect((await send(hi)).chunks).toEqual([
      { kind: "rejected", reason: "stale_epoch" },
    ]);
    expect(calls).toHaveLength(0);
  });

  test("a provider using threadsFetch re-checks at its real send point", async () => {
    let lost = false;
    const transport = recordingFetch([sse([])]);
    const providerFetch = threadsFetch(transport.fetch);
    const { send } = bridge(
      [
        async () => {
          lost = true; // the lease is lost while the provider prepares its request
          await providerFetch("https://acme.dev/chat", {
            method: "POST",
            body: "{}",
          });
          return [finish("stop")];
        },
      ],
      () => !lost,
    );
    const { chunks, thrown } = await send(hi);
    expect(transport.calls).toHaveLength(0);
    expect(thrown).toBeUndefined();
    expect(chunks).toEqual([{ kind: "rejected", reason: "stale_epoch" }]);
  });
});
