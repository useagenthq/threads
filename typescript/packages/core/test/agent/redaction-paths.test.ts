import { describe, expect, test } from "bun:test";
import { mkdtempSync, readdirSync, readFileSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { z } from "zod";
import {
  agent,
  extension,
  type Model,
  scriptedModel,
  sqlite,
  tool,
} from "../../src";
import { credential } from "../../src/agent/secret";
import { redactingSink, redactStream } from "../../src/redact";
import { memoryArtifacts } from "../../src/store/artifacts";

// C5, round 4: a value overlapping a longer one across stream chunks, model output (parts and
// deltas), hook text, extension setup errors and the run's own input are all recorded redacted.
// Every end-to-end case scans every file of a directory store and every request the model got.

const usage = { input_tokens: 1, output_tokens: 1 };
const use = (name: string, id: string) => ({
  content: [{ type: "tool_use", call_id: id, name, input: {} }],
  stop_reason: "tool_use",
  usage,
});
const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});
const decoder = new TextDecoder();

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? files(path) : [path];
  });
}

/** A scripted model that keeps each request body it is sent. */
function recording(responses: readonly unknown[]): {
  readonly model: Model;
  readonly sent: () => readonly string[];
} {
  const sent: string[] = [];
  const model = scriptedModel({ responses: [...responses] });
  const send = model.send;
  // Kept in place: the model-request guard knows the scripted model by identity.
  Object.assign(model, {
    send: (...args: Parameters<Model["send"]>) => {
      sent.push(decoder.decode(args[0].body));
      return send(...args);
    },
  });
  return { model, sent: () => sent };
}

/** Everything a run left behind: every file of its store, every request its model got. */
function nothingHolds(dir: string, sent: readonly string[], key: string): void {
  for (const path of files(dir))
    expect(readFileSync(path).includes(key)).toBe(false);
  for (const body of sent) expect(body).not.toContain(key);
}

const storeDir = (): string =>
  join(mkdtempSync(join(tmpdir(), "threads-redact4-")), "store");

/** `abc-lane9` and `abc-lane9-123`, registered for one test (each test starts with none). */
function overlapping(): { readonly short: string; readonly long: string } {
  return {
    short: credential("short", "apiKey", "abc-lane9", "U")(),
    long: credential("long", "apiKey", "abc-lane9-123", "U")(),
  };
}

describe("streaming redaction never leaks across a chunk boundary", () => {
  test("a value that a longer one continues is held until the longer one is decided", () => {
    const { long } = overlapping();
    const stream = redactStream();
    const shown =
      stream.feed("x abc-lane9") + stream.feed("-123 y") + stream.end();
    expect(shown).toBe("x [secret long.apiKey] y");
    expect(shown).not.toContain(long);
  });

  test("the final flush decides a held prefix", () => {
    const { short } = overlapping();
    const stream = redactStream();
    expect(stream.feed(`x ${short}`) + stream.end()).toBe(
      "x [secret short.apiKey]",
    );
  });

  test("a value split inside a multi-byte character is still replaced", async () => {
    const { long } = overlapping();
    const key = credential("fake", "apiKey", "ключ-l9-секрет", "U")();
    const artifacts = memoryArtifacts();
    const sink = redactingSink(artifacts.sink());
    const raw = new TextEncoder().encode(`a ${key} b ${long}`);
    for (const byte of raw) sink.write(Uint8Array.of(byte));
    const got = await artifacts.get((await sink.finish())?.sha256 ?? "");
    if (!got.ok) throw new Error(got.error.message);
    expect(decoder.decode(got.value)).toBe(
      "a [secret fake.apiKey] b [secret long.apiKey]",
    );
  });
});

describe("every recorded path is redacted (C5)", () => {
  test("model output: stored parts, streamed deltas and requests", async () => {
    const key = credential("fake", "apiKey", "sk-l9-echo-1a2b", "U")();
    const { model, sent } = recording([say(`the key is ${key}`), say("ok")]);
    const bot = agent({ model });
    const dir = storeDir();
    const store = sqlite(dir);
    const streamed = bot.stream("go", { store });
    let deltas = "";
    for await (const item of streamed)
      if (item.kind === "delta") deltas += item.text;
    const first = await streamed.result;
    expect(deltas).toBe("the key is [secret fake.apiKey]");
    const again = await bot.run("more", { store, thread: first.thread });
    expect(again.status).toBe("completed");
    nothingHolds(dir, sent(), key);
  });

  test("the run's own input", async () => {
    const key = credential("fake", "apiKey", "sk-l9-input-3c4d", "U")();
    const { model, sent } = recording([say("ok")]);
    const dir = storeDir();
    await agent({ model }).run(`my key is ${key}`, { store: sqlite(dir) });
    expect(sent().some((b) => b.includes("[secret fake.apiKey]"))).toBe(true);
    nothingHolds(dir, sent(), key);
  });

  test("hook text: an injected session_start line and a failing hook's reason", async () => {
    const key = credential("fake", "apiKey", "sk-l9-hook-5e6f", "U")();
    const noop = tool({
      name: "noop",
      description: "Nothing.",
      input: z.object({}),
      effect: "read_only",
      execute: async () => "ok",
    });
    const hooked = extension({
      name: "audit",
      hooks: {
        sessionStart: async () => [`context ${key}`],
        beforeTool: async () => {
          throw new Error(`gate down ${key}`);
        },
      },
    });
    const { model, sent } = recording([use("noop", "c1"), say("ok")]);
    const dir = storeDir();
    await agent({
      model,
      tools: [noop],
      extensions: [hooked],
      permissions: { mode: "bypass" },
    }).run("go", { store: sqlite(dir) });
    nothingHolds(dir, sent(), key);
  });

  test("an extension setup error returned by check()", async () => {
    const key = credential("fake", "apiKey", "sk-l9-setup-7a8b", "U")();
    const checked = await agent({
      model: scriptedModel({ responses: [] }),
      extensions: [
        extension({
          name: "boot",
          setup: async () => {
            throw new Error(`login failed for ${key}`);
          },
        }),
      ],
    }).check();
    expect(checked.ok ? "" : checked.error.message).toContain(
      "login failed for [secret fake.apiKey]",
    );
    expect(checked.ok ? "" : checked.error.message).not.toContain(key);
  });
});
