import { describe, expect, test } from "bun:test";
import { drain, renderBody } from "@threads/adapter-testkit";
import { memoryContext } from "@threads/core/adapter";
import { openai } from "../src";

// Live gate: real network, so it runs only with THREADS_LIVE=1, a key and
// THREADS_LIVE_OPENAI_MODEL (the caller picks the model; there is no default).

const key = process.env["OPENAI_API_KEY"];
const name = process.env["THREADS_LIVE_OPENAI_MODEL"];
const live =
  process.env["THREADS_LIVE"] === "1" &&
  key !== undefined &&
  name !== undefined;

describe.skipIf(!live)("live gate: openai", () => {
  test("one real attempt streams text and reports usage", async () => {
    const model = openai(name ?? "", {
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
    const done = chunks.at(-1);
    expect(done?.kind).toBe("done");
    if (done?.kind === "done")
      expect(done.usage.output_tokens).toBeGreaterThan(0);
  });
});
