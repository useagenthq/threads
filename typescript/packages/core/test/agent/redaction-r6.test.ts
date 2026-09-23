import { describe, expect, test } from "bun:test";
import {
  mkdtempSync,
  readdirSync,
  readFileSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  agent,
  ConfigError,
  localKnowledge,
  type McpServer,
  type Model,
  scriptedModel,
  secret,
  sqlite,
} from "../../src";
import { credential } from "../../src/agent/secret";
import type { KnownEvent } from "../../src/log";
import { resume } from "../../src/loop";
import { CONTEXT_DEFAULTS } from "../../src/loop/policy";
import { markTestKit } from "../../src/model/guard";
import { containsSecret, redactSecrets, redactStrings } from "../../src/redact";
import { events, harness, userInput } from "../loop/harness";
import {
  fixture,
  userInput as input,
  ROOT,
  started,
  THREAD,
  turnCompleted,
  unwrap,
} from "../store/helpers";

// C5, round 6 (Codex #328): registration rules, the post-redaction re-scan, the byte-exact
// fail-closed check (escaped JSON included), redacted setup failures of any kind, an atomic
// end to a provider-secret attempt, knowledge ingest and import refusal.

const usage = { input_tokens: 1, output_tokens: 1 };
const encoder = new TextEncoder();

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? files(path) : [path];
  });
}

const tempDir = (): string => mkdtempSync(join(tmpdir(), "threads-r6-"));

function rejects(run: () => unknown, message: string): void {
  expect(run).toThrow(message);
  try {
    run();
  } catch (error) {
    expect(error instanceof ConfigError && error.code).toBe("invalid_config");
  }
}

describe("registration rules", () => {
  test("a value shorter than 8 characters is refused", () => {
    rejects(
      () => credential("fake", "apiKey", "api", "U")(),
      "secret values must be at least 8 characters",
    );
    process.env["THREADS_TEST_SHORT"] = "1234567";
    rejects(
      () => secret("THREADS_TEST_SHORT").reveal(),
      "secret values must be at least 8 characters",
    );
    delete process.env["THREADS_TEST_SHORT"];
  });

  test("a value equal to a schema literal is refused", () => {
    for (const literal of ["user_input", "model_request", "provider_error"])
      rejects(
        () => credential("fake", "apiKey", literal, "U")(),
        "a secret value can't be a literal of the event schema",
      );
  });
});

describe("redacted output never holds a registered value", () => {
  test("a marker joined to its neighbour is re-scanned (#328 HIGH 1)", () => {
    credential("y", "apiKey", "abc[secret x.apiKey]", "U")();
    credential("x", "apiKey", "TOKEN-1234", "U")();
    expect(redactSecrets("abcTOKEN-1234")).toBe("[redacted]");
  });

  test("a numbered key that would hold a value is skipped (#328 HIGH 2)", () => {
    credential("x", "apiKey", "A-value-12", "U")();
    credential("z", "apiKey", "x.apiKey] (2)", "U")();
    const out = redactStrings({ "A-value-12": 1, "[secret x.apiKey]": 2 });
    expect(Object.keys(out ?? {})).toEqual([
      "[secret x.apiKey]",
      "[secret x.apiKey] (3)",
    ]);
  });
});

describe("containsSecret matches raw and JSON-escaped forms (#328 HIGH 3)", () => {
  test("quotes, backslashes, controls and non-ASCII", () => {
    const value = 'q"uo\\te\nd-é-8';
    credential("fake", "apiKey", value, "U")();
    const ascii = JSON.stringify({ v: value }).replace(
      /[\u0080-￿]/g,
      (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`,
    );
    for (const text of [
      `raw ${value}`,
      JSON.stringify({ v: value }),
      JSON.stringify({ [value]: 1 }),
      ascii,
    ])
      expect(containsSecret(encoder.encode(text))).toBe(true);
    expect(containsSecret(encoder.encode("nothing here"))).toBe(false);
  });
});

/** A test-kit model that puts provider material holding `key`, then finishes. */
function leaky(key: string, before: number = 0): Model {
  let sends = 0;
  const model: Model = {
    info: scriptedModel({ responses: [] }).info,
    send: async function* (_request, context) {
      sends += 1;
      if (sends <= before) {
        yield { kind: "part", part: { type: "text", text: "one" } };
        yield {
          kind: "done",
          stop_reason: "end_turn",
          usage: { input_tokens: 160_000, output_tokens: 5 },
        };
        return;
      }
      await context.put(
        encoder.encode(JSON.stringify({ encrypted: key })),
        "application/json",
      );
      yield { kind: "done", stop_reason: "end_turn", usage };
    },
  };
  markTestKit(model);
  return model;
}

describe("a provider-secret attempt ends in one batch (#328 HIGH 4)", () => {
  test("a takeover right after the abandonment still finds the turn ended", async () => {
    const key = credential("fake", "apiKey", "sk-l9-atomic-1a2b", "U")();
    const h = harness([], [], []);
    const writer = unwrap(h.store.acquire(ROOT, "owner", 30_000));
    await resume(
      writer,
      h.artifacts,
      h.config({
        models: () => leaky(key),
        onEvent: (e: KnownEvent) => {
          if (e.type !== "model_attempt_abandoned") return;
          h.clock.now += 60_000;
          unwrap(h.store.acquire(ROOT, "usurper"));
        },
      }),
      { input: userInput("go") },
    );
    const ended = events(writer).filter((e) => e.type === "turn_completed");
    expect(ended.map((e) => e.data)).toEqual([
      { reason: "error", code: "secret_in_provider_output" },
    ]);
  });

  test("a compaction request ends its turn the same way", async () => {
    const key = credential("fake", "apiKey", "sk-l9-compact-3c4d", "U")();
    const h = harness([], [], [], undefined, {
      context: {
        ...CONTEXT_DEFAULTS,
        compact: { ...CONTEXT_DEFAULTS.compact, keep_tail: { tokens: 1 } },
      },
    });
    const model = leaky(key, 1);
    for (const text of ["first", "second"]) {
      const writer = unwrap(h.store.acquire(ROOT, `owner-${text}`));
      await resume(writer, h.artifacts, h.config({ models: () => model }), {
        input: userInput(text),
      });
      writer.release();
    }
    const log = events(unwrap(h.store.acquire(ROOT, "reader")));
    const last = log.findLast((e) => e.type === "turn_completed");
    expect(last?.data).toEqual({
      reason: "error",
      code: "secret_in_provider_output",
    });
    expect(log.some((e) => e.type === "compaction_failed")).toBe(false);
  });
});

describe("setup failures of any kind are redacted values (#328 HIGH 5)", () => {
  test("an adapter setup that throws a plain Error", async () => {
    const key = credential("fake", "apiKey", "sk-l9-plain-5e6f", "U")();
    const model: Model = Object.assign(scriptedModel({ responses: [] }), {
      setup: async () => {
        throw new Error(`boom ${key}`);
      },
    });
    const bot = agent({ model });
    const checked = await bot.check();
    expect(checked).toMatchObject({
      ok: false,
      error: { code: "invalid_config" },
    });
    const message = checked.ok ? "" : checked.error.message;
    expect(message).toContain("boom [secret fake.apiKey]");
    expect(message).not.toContain(key);
    await expect(
      bot.run("go", { store: sqlite(":memory:") }),
    ).rejects.toMatchObject({ code: "invalid_config" });
  });

  test("an MCP connect that throws a plain Error", async () => {
    const key = credential("fake", "apiKey", "sk-l9-mcpx-7a8b", "U")();
    const server: McpServer = {
      kind: "mcp",
      name: "down",
      connect: async () => {
        throw new Error(`refused ${key}`);
      },
    };
    const checked = await agent({
      model: scriptedModel({ responses: [] }),
      tools: [server],
    }).check();
    expect(checked).toMatchObject({
      ok: false,
      error: { code: "mcp_unreachable" },
    });
    expect(checked.ok ? "" : checked.error.message).not.toContain(key);
  });
});

describe("byte-exact writes fail closed", () => {
  test("a knowledge source holding a value is not ingested (#328 HIGH 7)", async () => {
    const key = credential("fake", "apiKey", "sk-l9-doc-9c0d", "U")();
    const dir = tempDir();
    const doc = join(dir, "notes.md");
    writeFileSync(doc, `the key is ${key}`);
    const store = join(dir, "store");
    const run = agent({
      model: scriptedModel({ responses: [] }),
      knowledge: localKnowledge({ paths: [doc] }),
    }).run("go", { store: sqlite(store) });
    await expect(run).rejects.toMatchObject({ code: "invalid_config" });
    for (const path of files(store))
      expect(readFileSync(path).includes(key)).toBe(false);
  });

  test("an import holding a value is refused (the torn tail included)", () => {
    const later = "sk-l9-import-1e2f";
    const f = fixture();
    unwrap(f.store.createBranch(THREAD, ROOT));
    const writer = unwrap(f.store.acquire(ROOT, "holder"));
    unwrap(writer.append([started, input(`note ${later}`), turnCompleted]));
    const bytes = unwrap(f.store.exportBranch(ROOT));
    credential("fake", "apiKey", later, "U")();
    const imported = fixture().store.importLog(bytes);
    expect(imported.ok ? "ok" : imported.error.code).toBe(
      "secret_in_stored_bytes",
    );
  });
});
