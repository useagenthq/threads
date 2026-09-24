import { describe, expect, test } from "bun:test";
import { drain, renderBody } from "@threads/adapter-testkit";
import { memoryContext } from "@threads/core/adapter";
import { type AiSdkOptions, aiSdk } from "../src";

// Live gate: the bridge is qualified per provider package, which this repo
// doesn't install. Point THREADS_LIVE_AI_SDK at a module whose default export is a factory
// `(fetch) => LanguageModelV4` that builds its provider with that fetch, and set THREADS_LIVE=1.

const target = process.env["THREADS_LIVE_AI_SDK"];
const live = process.env["THREADS_LIVE"] === "1" && target !== undefined;

describe.skipIf(!live)("live gate: ai-sdk", () => {
  test("one real attempt streams to done", async () => {
    const mod: { default: AiSdkOptions["model"] } = await import(target ?? "");
    const model = aiSdk({
      model: mod.default,
      maxInputTokens: 128_000,
      maxOutputTokens: 4096,
      maxTokens: 64,
    });
    const { adapter, params } = model.info;
    const body = renderBody([
      { adapter, model: model.info.model, params, system: "", tools: [] },
      { role: "user", content: [{ type: "text", text: "Say ok." }] },
    ]);
    const { chunks, thrown } = await drain(
      model.send({ request_id: "live:1", body }, memoryContext()),
    );
    expect(thrown).toBeUndefined();
    expect(chunks.at(-1)?.kind).toBe("done");
  });
});
