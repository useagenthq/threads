import { describe, expect, test } from "bun:test";
import {
  drain,
  recordingFetch,
  renderBody,
  sse,
} from "@threads/adapter-testkit";
import { agent, type Model, openThread, sqlite } from "@threads/core";
import { markTestKit, memoryContext } from "@threads/core/adapter";
import { z } from "zod";
import { type OpenAIOptions, openai } from "../src";

// Lane 06: openai("<id>") takes its limits from spec/models/openai.v1.json and pins the cap under
// the provider-neutral params.max_tokens, which output budgets read; the Responses API gets it
// as max_output_tokens.

const done = (text: string) =>
  sse([
    {
      event: "response.output_item.done",
      data: {
        type: "response.output_item.done",
        output_index: 0,
        item: {
          id: "msg_1",
          type: "message",
          role: "assistant",
          status: "completed",
          content: [{ type: "output_text", text, annotations: [] }],
        },
      },
    },
    {
      event: "response.completed",
      data: {
        type: "response.completed",
        response: {
          status: "completed",
          usage: { input_tokens: 5, output_tokens: 2, total_tokens: 7 },
        },
      },
    },
  ]);

function gpt(options: OpenAIOptions = {}): {
  readonly model: Model;
  readonly calls: { body: unknown }[];
} {
  const { fetch, calls } = recordingFetch([done("ok"), done("ok")]);
  const model = openai("gpt-5.5", { apiKey: "test-key", fetch, ...options });
  markTestKit(model);
  return { model, calls };
}

describe("openai(id)", () => {
  test("a listed id needs no options: verified limits and the default cap", () => {
    const { info } = openai("gpt-5.5");
    expect(info.params).toEqual({ max_tokens: 8192 });
    expect(info.limits).toEqual({
      provider: "openai",
      name: "gpt-5.5",
      context_window: 922_000,
      max_output_tokens: 128_000,
      input_billing_bound: "context_window",
    });
  });

  test("each option overrides one limit; an unlisted id needs both", () => {
    expect(openai("gpt-5.5", { maxTokens: 64_000 }).info.params).toEqual({
      max_tokens: 64_000,
    });
    expect(() => openai("gpt-7")).toThrow(
      'openai: unknown model "gpt-7"; pass maxInputTokens and maxOutputTokens',
    );
  });

  test.each(["max_tokens", "max_output_tokens"])(
    "params can't set %s: the cap is maxTokens",
    (key) => {
      expect(() => openai("gpt-5.5", { params: { [key]: 10 } })).toThrow(
        `openai params can't set ${key}: pass maxTokens`,
      );
    },
  );

  test("the wire body's max_output_tokens is the pinned params.max_tokens", async () => {
    const { model, calls } = gpt({ maxTokens: 2048 });
    const { adapter, params } = model.info;
    const body = renderBody([
      { adapter, model: model.info.model, params, system: "", tools: [] },
      { role: "user", content: [{ type: "text", text: "hi" }] },
    ]);
    await drain(model.send({ request_id: "b:e1", body }, memoryContext()));
    const sent = z.looseObject({}).parse(calls[0]?.body);
    expect(params).toEqual({ max_tokens: 2048 });
    expect(sent["max_output_tokens"]).toBe(2048);
    expect("max_tokens" in sent).toBe(false);
  });

  test("an output-token budget is enforceable with the default cap", async () => {
    const bot = agent({
      model: gpt().model,
      budget: { max_output_tokens: 50_000 },
    });
    expect(await bot.check()).toEqual({ ok: true, value: undefined });
  });
});

describe("continuing a thread", () => {
  test("explicit limits equal to the catalog's continue it; other limits don't", async () => {
    const store = sqlite(":memory:");
    const first = await agent({ model: gpt().model }).run("hi", { store });
    expect(first.status).toBe("completed");
    // What a withdrawal's message prints: the corrected values start new threads...
    const corrected = agent({ model: gpt({ maxInputTokens: 900_000 }).model });
    await expect(
      corrected.run("again", { store, thread: first.thread }),
    ).rejects.toMatchObject({ code: "invalid_config" });
    // ...and the original values continue this one.
    const original = agent({
      model: gpt({ maxInputTokens: 922_000, maxOutputTokens: 128_000 }).model,
    });
    const second = await original.run("again", { store, thread: first.thread });
    expect(second.status).toBe("completed");
  });

  test("a thread started before the catalog (no pinned cap) can't continue, and stays readable", async () => {
    const store = sqlite(":memory:");
    const { model } = gpt();
    // The factory before lane 06 pinned no cap for OpenAI.
    const before: Model = { ...model, info: { ...model.info, params: {} } };
    markTestKit(before);
    const first = await agent({ model: before }).run("hi", { store });
    expect(first.status).toBe("completed");
    const thread = await openThread(store, first.thread.id);
    if (!thread.ok) throw new Error(thread.error.message);
    const entries = async (t: typeof thread.value) => {
      const timeline = await t.timeline();
      if (!timeline.ok) throw new Error(timeline.error.message);
      return timeline.value.entries.length;
    };
    const count = await entries(thread.value);
    await expect(
      agent({ model: gpt().model }).run("again", {
        store,
        thread: first.thread,
      }),
    ).rejects.toMatchObject({ code: "invalid_config" });
    const after = await openThread(store, first.thread.id);
    if (!after.ok) throw new Error(after.error.message);
    expect(await entries(after.value)).toBe(count);
    expect((await after.value.cost()).ok).toBe(true);
  });
});
