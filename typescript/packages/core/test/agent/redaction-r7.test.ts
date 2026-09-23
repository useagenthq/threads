import { describe, expect, test } from "bun:test";
import { agent, type McpServer, scriptedModel, sqlite } from "../../src";
import { credential } from "../../src/agent/secret";
import type { McpSession } from "../../src/agent/setup";
import { sha256Hex } from "../../src/hash";
import {
  bindLocalKnowledge,
  localKnowledge,
} from "../../src/memory/local-knowledge";
import { redactingSink, register } from "../../src/redact";
import { type EventDraft, memoryArtifacts } from "../../src/store";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import {
  fixture,
  userInput as input,
  ROOT,
  started,
  THREAD,
  unwrap,
} from "../store/helpers";

// C5, round 7 (Codex #359): the stored line's canonical bytes, MCP failures of any shape, a
// value registered while a spill streams, direct knowledge ingest, and a torn-tail import.

const encoder = new TextEncoder();
const decoder = new TextDecoder();

function writer() {
  const f = fixture();
  unwrap(f.store.createBranch(THREAD, ROOT));
  return { f, w: unwrap(f.store.acquire(ROOT, "holder")) };
}

describe("the writer checks the canonical bytes it stores (#359 HIGH 1)", () => {
  test("a marker whose quote canonical JSON escapes into another value", () => {
    register("FIRSTVALUE", 'X"Y');
    register('[secret X\\"Y]', "Z");
    const { f, w } = writer();
    unwrap(w.append([started]));
    const appended = w.append([input("FIRSTVALUE")]);
    expect(appended.ok ? "ok" : appended.error.code).toBe(
      "secret_in_stored_bytes",
    );
    const bytes = unwrap(f.store.exportBranch(ROOT));
    expect(decoder.decode(bytes)).not.toContain('[secret X\\"Y]');
  });

  test("separate strings that JSON punctuation joins into a value", () => {
    register('alpha","beta', "Z");
    const { w } = writer();
    const joined: EventDraft = {
      type: "thread_started",
      type_version: 1,
      critical: true,
      actor: { kind: "host" },
      data: {
        agent_name: "demo",
        config_hash: "a".repeat(64),
        model: { provider: "scripted", name: "scripted-1" },
        model_params: { max_tokens: 1024 },
        adapter: {
          name: "s",
          version: "1",
          settings: { t: ["alpha", "beta"] },
        },
        instructions: "You are a helpful agent.",
        tools: [],
      },
    };
    const appended = w.append([joined]);
    expect(appended.ok ? "ok" : appended.error.code).toBe(
      "secret_in_stored_bytes",
    );
  });

  test("a run whose model output the writer refuses fails with that code", async () => {
    register("FIRSTVALUE", 'X"Y');
    register('[secret X\\"Y]', "Z");
    const text = { type: "text", text: "a FIRSTVALUE" } as const;
    const model = scriptedModel({
      responses: [
        {
          content: [text],
          stop_reason: "end_turn",
          usage: { input_tokens: 1, output_tokens: 1 },
        },
      ],
    });
    const result = await agent({ model }).run("go", {
      store: sqlite(":memory:"),
    });
    expect(result).toMatchObject({
      status: "failed",
      error: { code: "secret_in_stored_bytes" },
    });
  });
});

function session(close: () => Promise<void>): McpSession {
  return { tools: [], close, [Symbol.asyncDispose]: close };
}

const bot = (...servers: McpServer[]) =>
  agent({ model: scriptedModel({ responses: [] }), tools: servers });

describe("MCP failures of any shape are redacted values (#359 HIGH 2)", () => {
  test("a connect that throws before returning a promise", async () => {
    const key = credential("fake", "apiKey", "sk-l9-sync-1a2b", "U")();
    const checked = await bot({
      kind: "mcp",
      name: "sync",
      connect: () => {
        throw new Error(`refused ${key}`);
      },
    }).check();
    expect(checked).toMatchObject({
      ok: false,
      error: { code: "mcp_unreachable" },
    });
    expect(checked.ok ? "" : checked.error.message).not.toContain(key);
  });

  test("a sibling whose close fails never replaces the redacted failure", async () => {
    const key = credential("fake", "apiKey", "sk-l9-close-3c4d", "U")();
    const up: McpServer = {
      kind: "mcp",
      name: "up",
      connect: async () =>
        session(async () => {
          throw new Error(`close ${key}`);
        }),
    };
    const down: McpServer = {
      kind: "mcp",
      name: "down",
      connect: async () => {
        throw new Error(`refused ${key}`);
      },
    };
    const checked = await bot(up, down).check();
    expect(checked).toMatchObject({
      ok: false,
      error: { code: "mcp_unreachable" },
    });
    expect(checked.ok ? "" : checked.error.message).not.toContain(key);
    expect(await bot(up).check()).toEqual({ ok: true, value: undefined });
  });
});

describe("a value registered while a spill streams (#359 HIGH 3)", () => {
  test("the spill is dropped, never committed across the value", () => {
    const artifacts = memoryArtifacts();
    const sink = redactingSink(artifacts.sink());
    sink.write(encoder.encode("abcd"));
    register("abcdefgh", "late");
    sink.write(encoder.encode("efgh"));
    expect(sink.finish()).toBeUndefined();
    expect(artifacts.get(sha256Hex(encoder.encode("abcdefgh"))).ok).toBe(false);
  });
});

describe("the local knowledge provider refuses a source holding a value (#359 HIGH 4)", () => {
  test("a direct ingest stores nothing", async () => {
    const key = credential("fake", "apiKey", "sk-l9-ingest-5e6f", "U")();
    const artifacts = memoryArtifacts();
    const bound = bindLocalKnowledge(
      localKnowledge({ paths: [] }),
      openBunSqlite(":memory:"),
      artifacts,
    );
    if (bound === undefined) throw new Error("localKnowledge binds");
    const content = encoder.encode(`the key is ${key}`);
    const scope = { tenant_id: "t", agent: "a", scope: "u" };
    const got = await bound.local.ingest(
      scope,
      {
        source_id: "doc.md",
        media_type: "text/markdown",
        content,
        binding: { namespace: "n", record_id: "doc" },
      },
      "doc@1",
    );
    expect(got.ok ? "ok" : got.error.code).toBe("invalid");
    expect(unwrap(await bound.local.search(scope, "key"))).toEqual([]);
  });
});

describe("an import's torn tail is checked (#359 MEDIUM)", () => {
  test("a torn tail holding a value refuses the import", () => {
    const later = "sk-l9-torn-7a8b";
    const { f, w } = writer();
    unwrap(w.append([started, input("hi")]));
    const bytes = unwrap(f.store.exportBranch(ROOT));
    const body = bytes.subarray(
      0,
      bytes.lastIndexOf(0x0a, bytes.length - 2) + 1,
    );
    const tail = encoder.encode(`{"torn":"${later}`);
    const torn = new Uint8Array([...body, ...tail]);
    expect(unwrap(fixture().store.importLog(torn)).torn?.bytes).toEqual(tail);
    credential("fake", "apiKey", later, "U")();
    const imported = fixture().store.importLog(torn);
    expect(imported.ok ? "ok" : imported.error.code).toBe(
      "secret_in_stored_bytes",
    );
  });
});
