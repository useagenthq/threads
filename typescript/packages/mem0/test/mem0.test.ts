import { expect, test } from "bun:test";
import { agent, scriptedModel, secret } from "@threads/core";
import { mem0 } from "../src";

// mem0's SDK can't be fenced at its transport, so the provider is refused at setup (
// item 3): the agent never starts with a memory that a stale writer could still write.

test("mem0() is refused at setup with transport_fence_unsupported", async () => {
  const checked = await agent({
    model: scriptedModel({ responses: [] }),
    memory: mem0({ apiKey: secret("MEM0_API_KEY") }),
  }).check();
  expect(checked).toMatchObject({
    ok: false,
    error: { code: "transport_fence_unsupported" },
  });
});
