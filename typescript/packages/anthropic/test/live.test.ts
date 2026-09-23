import { describe, expect, test } from "bun:test";
import { drain, renderBody } from "@threads/adapter-testkit";
import { memoryContext } from "@threads/core/adapter";
import { anthropic } from "../src";

// Live gate: real network, so it runs only with THREADS_LIVE=1 and a key.
// Set THREADS_LIVE_ANTHROPIC_MODEL to pick the model.

const key = process.env["ANTHROPIC_API_KEY"];
const live = process.env["THREADS_LIVE"] === "1" && key !== undefined;
const name = process.env["THREADS_LIVE_ANTHROPIC_MODEL"] ?? "claude-sonnet-5";

describe.skipIf(!live)("live gate: anthropic", () => {
  test("one real attempt streams text and reports usage", async () => {
    const model = anthropic({
      model: name,
      maxTokens: 64,
      contextWindow: 200_000,
      maxOutputTokens: 64_000,
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
    const done = chunks.at(-1);
    expect(done?.kind).toBe("done");
    if (done?.kind === "done")
      expect(done.usage.input_tokens).toBeGreaterThan(0);
  });
});
