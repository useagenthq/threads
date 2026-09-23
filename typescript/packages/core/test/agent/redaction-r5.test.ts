import { describe, expect, test } from "bun:test";
import { mkdtempSync, readdirSync, readFileSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { z } from "zod";
import {
  agent,
  ConfigError,
  type McpServer,
  type Model,
  scriptedModel,
  sqlite,
  tool,
} from "../../src";
import { credential } from "../../src/agent/secret";
import { redactSecrets } from "../../src/redact";
import { webFetch } from "../../src/tools/web-fetch";
import type { WebTransport } from "../../src/tools/web-transport";
import { bound } from "../tools/kit";

// C5, round 5: the marker never repeats a registered value, the writer redacts JSON keys and
// the actor, web_fetch stores its cited page redacted, and adapter and MCP setup errors are
// returned redacted.

const usage = { input_tokens: 1, output_tokens: 1 };
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

const storeDir = (): string =>
  join(mkdtempSync(join(tmpdir(), "threads-redact5-")), "store");

function nothingHolds(dir: string, key: string): void {
  for (const path of files(dir))
    expect(readFileSync(path).includes(key)).toBe(false);
}

describe("the marker never repeats a registered value", () => {
  test("a value inside its own label falls back to a plain marker", () => {
    credential("fake", "apiKey", "api", "U")();
    expect(redactSecrets("my api here")).toBe("my [secret] here");
  });

  test("a value inside the plain marker falls back again", () => {
    credential("x", "apiKey", "secret", "U")();
    expect(redactSecrets("a secret b")).toBe("a [redacted] b");
  });
});

describe("the writer redacts every string of the line", () => {
  test("a registered value as a JSON key and inside the actor", async () => {
    const key = credential("fake", "apiKey", "sk-l9-key-4d5e", "U")();
    const any = tool({
      name: "any",
      description: "Takes anything.",
      input: z.record(z.string(), z.unknown()),
      effect: "read_only",
      execute: async () => "ok",
    });
    const dir = storeDir();
    await agent({
      model: scriptedModel({
        responses: [
          {
            content: [
              {
                type: "tool_use",
                call_id: "c1",
                name: "any",
                input: { [key]: "x" },
              },
            ],
            stop_reason: "tool_use",
            usage,
          },
          say("ok"),
        ],
      }),
      tools: [any],
      permissions: { mode: "bypass" },
    }).run("go", {
      store: sqlite(dir),
      principal: { issuer: "api", tenant: "local", subject: key },
    });
    nothingHolds(dir, key);
  });
});

describe("web_fetch stores its cited page redacted", () => {
  test("the citation's artifact holds the label, not the value", async () => {
    const key = credential("fake", "apiKey", "sk-l9-page-6f7a", "U")();
    const transport: WebTransport = {
      resolve: async () => ["93.184.216.34"],
      fetch: async () =>
        new Response(`the key is ${key}`, {
          status: 200,
          headers: { "content-type": "text/plain" },
        }),
    };
    const { artifacts, run } = bound(webFetch(transport));
    const got = await run({ url: "https://example.com/page" });
    if (got.kind !== "done") throw new Error(got.kind);
    const cited = got.content?.find((p) => p.type === "citation");
    const ref = cited?.type === "citation" ? cited.ref : undefined;
    if (ref === undefined) throw new Error("web_fetch cites its page");
    const stored = artifacts.get(ref.sha256);
    if (!stored.ok) throw new Error(stored.error.message);
    expect(decoder.decode(stored.value)).toBe(
      "the key is [secret fake.apiKey]",
    );
    expect(ref.bytes).toBe(stored.value.length);
  });
});

describe("setup errors are returned redacted", () => {
  test("an adapter setup error, from check() and from a run", async () => {
    const key = credential("fake", "apiKey", "sk-l9-adapter-8b9c", "U")();
    const model: Model = Object.assign(scriptedModel({ responses: [] }), {
      setup: async () => {
        throw new ConfigError("invalid_config", `rejected ${key}`);
      },
    });
    const bot = agent({ model });
    const checked = await bot.check();
    expect(checked.ok ? "" : checked.error.message).toBe(
      "rejected [secret fake.apiKey]",
    );
    await expect(
      bot.run("go", { store: sqlite(":memory:") }),
    ).rejects.toMatchObject({ message: "rejected [secret fake.apiKey]" });
  });

  test("an MCP connect error, from check() and from a run", async () => {
    const key = credential("fake", "apiKey", "sk-l9-mcp-0d1e", "U")();
    const server: McpServer = {
      kind: "mcp",
      name: "down",
      connect: async () => {
        throw new ConfigError("mcp_unreachable", `401 for ${key}`);
      },
    };
    const bot = agent({
      model: scriptedModel({ responses: [] }),
      tools: [server],
    });
    const checked = await bot.check();
    expect(checked.ok ? "" : checked.error.message).toBe(
      "401 for [secret fake.apiKey]",
    );
    await expect(
      bot.run("go", { store: sqlite(":memory:") }),
    ).rejects.toMatchObject({ message: "401 for [secret fake.apiKey]" });
  });
});
