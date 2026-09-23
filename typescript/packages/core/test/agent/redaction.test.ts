import { describe, expect, test } from "bun:test";
import { mkdtempSync, readdirSync, readFileSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { z } from "zod";
import {
  agent,
  fakeSandbox,
  openThread,
  type RunResult,
  scriptedModel,
  sqlite,
  type Tool,
} from "../../src";
import { credential, redactingSink } from "../../src/agent/secret";
import { memoryArtifacts } from "../../src/store/artifacts";

// C5 on every path a resolved credential could be recorded by: a spilled exec output's full
// bytes (redacted as they stream, a key split across chunks included), the references a result
// injects, and every string of a result's parts, not only text.

const LABEL = "[secret fake.apiKey]";
const usage = { input_tokens: 1, output_tokens: 1 };
const use = (name: string, input: Record<string, unknown>, id: string) => ({
  content: [{ type: "tool_use", call_id: id, name, input }],
  stop_reason: "tool_use",
  usage,
});
const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? files(path) : [path];
  });
}

async function stored(result: RunResult<string>): Promise<string> {
  const thread = await openThread(result.thread.store, result.thread.id);
  if (!thread.ok) throw new Error(thread.error.message);
  const timeline = await thread.value.timeline();
  if (!timeline.ok) throw new Error(timeline.error.message);
  return JSON.stringify(timeline.value.entries);
}

describe("secret redaction on every recorded path (C5)", () => {
  test("a key split across chunks is redacted as the stream is recorded", () => {
    const key = credential("fake", "apiKey", "sk-lane09-split-7d2e", "U")();
    const artifacts = memoryArtifacts();
    const sink = redactingSink(artifacts.sink());
    const raw = new TextEncoder().encode(`before ${key} after`);
    for (let i = 0; i < raw.length; i += 3) sink.write(raw.subarray(i, i + 3));
    const { sha256, bytes } = sink.finish();
    const got = artifacts.get(sha256);
    if (!got.ok) throw new Error(got.error.message);
    expect(new TextDecoder().decode(got.value)).toBe(`before ${LABEL} after`);
    expect(bytes).toBe(got.value.length);
  });

  test("a spilled exec output is stored redacted and read back redacted", async () => {
    const key = credential("fake", "apiKey", "sk-lane09-42-spill", "U")();
    const box = fakeSandbox({
      tools: { bash: { output: `${"a".repeat(40_000)}${key}\n` } },
    });
    const dir = join(mkdtempSync(join(tmpdir(), "threads-redact-")), "store");
    const read = { call_id: "c1", offset: 39_990, length: 200 };
    const result = await agent({
      model: scriptedModel({
        responses: [
          use("bash", { command: "bash big" }, "c1"),
          use("read_tool_result", read, "c2"),
          say("done"),
        ],
      }),
      sandbox: box,
      permissions: { mode: "bypass" },
    }).run("go", { store: sqlite(dir) });
    expect(await stored(result)).toContain(LABEL);
    for (const path of files(dir))
      expect(readFileSync(path).includes(key)).toBe(false);
  });

  test("injected references and every string of a result's parts are redacted", async () => {
    const key = credential("fake", "apiKey", "sk-lane09-cite-5f10", "U")();
    const spec = {
      name: "recall",
      description: "Recall.",
      input_schema: { type: "object" },
      effect_class: "read_only",
    } as const;
    const recall: Tool<unknown, unknown, unknown> = {
      name: "recall",
      spec: () => spec,
      bind: () => ({
        spec,
        input: z.object({}),
        run: async () => ({
          kind: "done",
          output: "found",
          isError: false,
          content: [
            { type: "text", text: `note ${key}` },
            {
              type: "citation",
              source_kind: "web",
              source_id: `https://example.test/?k=${key}`,
              title: `title ${key}`,
              cited_text: `cited ${key}`,
            },
          ],
          inject: [
            {
              source: "memory",
              trust: "untrusted_reference",
              origin: { id: "m1", version: "1" },
              text: `the key is ${key}`,
            },
          ],
        }),
      }),
    };
    const result = await agent({
      model: scriptedModel({
        responses: [use("recall", {}, "c1"), say("done")],
      }),
      tools: [recall],
      permissions: { mode: "bypass" },
    }).run("go", { store: sqlite(":memory:") });
    const all = await stored(result);
    expect(all).toContain(`title ${LABEL}`);
    expect(all).toContain(`the key is ${LABEL}`);
    expect(all).not.toContain(key);
  });
});
