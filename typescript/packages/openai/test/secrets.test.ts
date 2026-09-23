import { describe, expect, test } from "bun:test";
import { mkdtempSync, readdirSync, readFileSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { recordingFetch, sse } from "@threads/adapter-testkit";
import { agent, sqlite } from "@threads/core";
import { credential, type Json, markTestKit } from "@threads/core/adapter";
import { openai } from "../src";

// Provider items are stored byte-exact for replay (reasoning is encrypted, hosted results are
// the provider's own), so they are never edited. One that holds a registered secret is never
// stored: the turn ends with secret_in_provider_output, and nothing on disk holds the value.

const ev = (data: { readonly type: string } & { [key: string]: Json }) => ({
  event: data.type,
  data,
});
const completed = ev({
  type: "response.completed",
  response: {
    status: "completed",
    usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 },
  },
});

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? files(path) : [path];
  });
}

async function run(item: { readonly [key: string]: Json }, key: string) {
  const { fetch } = recordingFetch([
    sse([
      ev({ type: "response.output_item.done", output_index: 0, item }),
      completed,
    ]),
  ]);
  const model = openai({
    model: "gpt-5.5",
    contextWindow: 400_000,
    maxOutputTokens: 128_000,
    hostedTools: [{ type: "web_search" }],
    apiKey: "sk-test-openai",
    fetch,
  });
  markTestKit(model);
  const dir = join(mkdtempSync(join(tmpdir(), "threads-oai-")), "store");
  const result = await agent({ model }).run("go", { store: sqlite(dir) });
  for (const path of files(dir))
    expect(readFileSync(path).includes(key)).toBe(false);
  return result;
}

describe("provider items holding a registered secret fail closed", () => {
  test("an encrypted reasoning item", async () => {
    const key = credential("fake", "apiKey", "sk-l9-reason-2f3a", "U")();
    const result = await run(
      {
        id: "rs_1",
        type: "reasoning",
        summary: [],
        encrypted_content: `gAAA${key}`,
      },
      key,
    );
    expect(result.status === "failed" && result.error.code).toBe(
      "secret_in_provider_output",
    );
  });

  test("a hosted web_search item", async () => {
    const key = credential("fake", "apiKey", "sk-l9-hosted-4b5c", "U")();
    const result = await run(
      {
        id: "ws_1",
        type: "web_search_call",
        status: "completed",
        action: { type: "search", query: `find ${key}` },
      },
      key,
    );
    expect(result.status === "failed" && result.error.code).toBe(
      "secret_in_provider_output",
    );
  });
});
