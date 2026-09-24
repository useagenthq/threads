import { afterEach, describe, expect, test } from "bun:test";
import {
  agent,
  ConfigError,
  type Exporter,
  scriptedModel,
  sqlite,
  tool,
} from "@threads/core";
import { storeConnection } from "@threads/core/host";
import { z } from "zod";
import { credential } from "../../core/src/agent/secret";
import { otel } from "../src";
import { accepted, attr, type Collector, collector } from "./collector";
import { cursors, exporter, forgetCursors, looping, say, use } from "./kit";

// Exporter.sync() against a live fake collector: each span once, when it closes; the cursor moves
// only after a 2xx; a collector that is down or refuses loses nothing.

let c: Collector;
afterEach(async () => {
  await c.stop();
});

/** Replaces every OTEL_* variable with `vars`; returns what was there, to restore. */
function withEnv(
  vars: Readonly<Record<string, string>>,
): Record<string, string> {
  const saved: Record<string, string> = {};
  for (const [k, v] of Object.entries(process.env))
    if (k.startsWith("OTEL_") && v !== undefined) {
      saved[k] = v;
      delete process.env[k];
    }
  Object.assign(process.env, vars);
  return saved;
}

async function unwrapped(e: Exporter): Promise<{ readonly spans: number }> {
  const r = await e.sync();
  if (!r.ok) throw new Error(`sync failed: ${r.error.code}`);
  return r.value;
}

describe("Exporter.sync", () => {
  test("a 40-model-call turn: every span arrives exactly once, the turn span after turn_completed", async () => {
    c = collector();
    const store = sqlite(":memory:");
    let e: Exporter | undefined;
    const bot = looping(40, async () => {
      if (e !== undefined) await unwrapped(e);
    });
    e = exporter(store, c.url);
    const result = await bot.run("Look everything up.", { store });
    expect(result.status).toBe("completed");
    expect(accepted(c).some((s) => s.name.startsWith("invoke_agent"))).toBe(
      false,
    );
    await unwrapped(e);
    await unwrapped(e);
    const ids = accepted(c).map((s) => s.spanId);
    expect(new Set(ids).size).toBe(ids.length);
    const names = accepted(c).map((s) => s.name);
    expect(names.filter((n) => n.startsWith("chat"))).toHaveLength(40);
    expect(names.filter((n) => n === "execute_tool lookup")).toHaveLength(39);
    expect(names.filter((n) => n.startsWith("invoke_agent"))).toHaveLength(1);
    expect(c.received.length).toBeGreaterThan(30);
  });

  test("a 5xx or a refused connection is collector_unavailable and moves no cursor", async () => {
    c = collector();
    const store = sqlite(":memory:");
    await looping(2).run("Hi.", { store });
    c.answers = [503];
    const down = await exporter(store, c.url).sync();
    expect(down).toMatchObject({
      ok: false,
      error: { code: "collector_unavailable", status: 503 },
    });
    expect(await cursors(store)).toEqual({});
    const refused = await exporter(
      store,
      "http://127.0.0.1:1/v1/traces",
    ).sync();
    expect(refused).toMatchObject({
      ok: false,
      error: { code: "collector_unavailable" },
    });
    expect(await cursors(store)).toEqual({});
  });

  test("a 400 is collector_rejected with its status and the start of its body", async () => {
    c = collector();
    const store = sqlite(":memory:");
    await looping(1).run("Hi.", { store });
    c.answers = [400];
    const sent = await exporter(store, c.url).sync();
    expect(sent).toMatchObject({
      ok: false,
      error: { code: "collector_rejected", status: 400 },
    });
    if (!sent.ok) expect(sent.error.message).toContain("bad spans");
    expect(await cursors(store)).toEqual({});
  });

  test("a failure before the 2xx re-sends the same bytes", async () => {
    c = collector();
    const store = sqlite(":memory:");
    await looping(2).run("Hi.", { store });
    const e = exporter(store, c.url);
    c.answers = [500];
    expect((await e.sync()).ok).toBe(false);
    await unwrapped(e);
    expect(c.attempts).toHaveLength(2);
    expect(c.attempts[1]).toBe(c.attempts[0] ?? "");
  });

  test("a crash after the 2xx, before the cursor write, re-sends the same span ids and nothing else", async () => {
    c = collector();
    const store = sqlite(":memory:");
    await looping(3).run("Hi.", { store });
    const e = exporter(store, c.url);
    await unwrapped(e);
    const before = accepted(c).map((s) => s.spanId);
    await forgetCursors(store);
    await unwrapped(e);
    expect(accepted(c).map((s) => s.spanId)).toEqual([...before, ...before]);
    await unwrapped(e);
    expect(accepted(c)).toHaveLength(before.length * 2);
  });

  test("gzip sends a body that decompresses to the uncompressed one", async () => {
    c = collector();
    const store = sqlite(":memory:");
    await looping(1).run("Hi.", { store });
    const plain = collector();
    try {
      await unwrapped(exporter(store, plain.url));
      process.env["OTEL_EXPORTER_OTLP_TRACES_COMPRESSION"] = "gzip";
      const zipped = otel({ store, endpoint: c.url, name: "zipped" });
      delete process.env["OTEL_EXPORTER_OTLP_TRACES_COMPRESSION"];
      await unwrapped(zipped);
      expect(c.received[0]?.headers.get("content-encoding")).toBe("gzip");
      expect(c.received[0]?.text).toBe(plain.received[0]?.text);
    } finally {
      await plain.stop();
    }
  });

  test("content is off by default; with content: true the logged bytes are sent", async () => {
    c = collector();
    const store = sqlite(":memory:");
    await looping(2).run("Hi.", { store });
    await unwrapped(exporter(store, c.url));
    const tool = accepted(c).find((s) => s.name === "execute_tool lookup");
    expect(tool && attr(tool, "gen_ai.tool.call.arguments")).toBeUndefined();
    const on = otel({ store, endpoint: c.url, name: "content", content: true });
    await unwrapped(on);
    const shown = accepted(c)
      .filter((s) => s.name === "execute_tool lookup")
      .at(-1);
    expect(shown && attr(shown, "gen_ai.tool.call.result")).toBe("found");
    expect(shown && attr(shown, "gen_ai.tool.call.arguments")).toBe("{}");
  });

  test("a registered secret in a tool input never reaches the collector, even with content", async () => {
    c = collector();
    const key = credential("fake", "apiKey", "sk-otel-lane23-9f1c", "U")();
    const store = sqlite(":memory:");
    const bot = agent({
      model: scriptedModel({
        responses: [use("c1", { q: key }), say("Done.")],
      }),
      tools: [
        tool({
          name: "lookup",
          description: "Look something up.",
          input: z.object({ q: z.string().optional() }),
          effect: "read_only",
          runs: "host",
          execute: async ({ q }) => `found ${q ?? ""}`,
        }),
      ],
    });
    await bot.run("Hi.", { store });
    await unwrapped(otel({ store, endpoint: c.url, content: true }));
    const text = c.received.map((r) => r.text).join("");
    expect(text).toContain("gen_ai.tool.call.arguments");
    expect(text).not.toContain(key);
  });

  test("the headers option is sent, and two named exporters each send every span", async () => {
    c = collector();
    const store = sqlite(":memory:");
    await looping(1).run("Hi.", { store });
    await unwrapped(
      otel({ store, endpoint: c.url, headers: { "x-team": "k1" } }),
    );
    await unwrapped(otel({ store, endpoint: c.url, name: "second" }));
    expect(c.received[0]?.headers.get("x-team")).toBe("k1");
    expect(c.received[1]?.spans.map((s) => s.spanId)).toEqual(
      c.received[0]?.spans.map((s) => s.spanId),
    );
    expect(await cursors(store, "otel:second")).toEqual(await cursors(store));
  });

  test("otel() with no endpoint anywhere is invalid_config naming both variables", () => {
    c = collector();
    const saved = withEnv({});
    try {
      expect(() => otel()).toThrow(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT or OTEL_EXPORTER_OTLP_ENDPOINT",
      );
      expect(() => otel()).toThrow(ConfigError);
    } finally {
      withEnv(saved);
    }
  });

  test("unset options come from the variables: headers, and service.name threads", async () => {
    c = collector();
    const store = sqlite(":memory:");
    await looping(1).run("Hi.", { store });
    const saved = withEnv({
      OTEL_EXPORTER_OTLP_HEADERS: "x-honeycomb-team=k%201",
    });
    try {
      await unwrapped(otel({ store, endpoint: c.url }));
    } finally {
      withEnv(saved);
    }
    expect(c.received[0]?.headers.get("x-honeycomb-team")).toBe("k 1");
    expect(c.received[0]?.text).toContain(
      '{"key":"service.name","value":{"stringValue":"threads"}}',
    );
  });

  test("without a store and outside a host, sync() is invalid_config", async () => {
    c = collector();
    await expect(otel({ endpoint: c.url }).sync()).rejects.toThrow(ConfigError);
  });

  test("a branch written by a second store handle on the same file is exported", async () => {
    c = collector();
    const dir = `${process.env["TMPDIR"] ?? "/tmp"}/threads-otel-${crypto.randomUUID()}`;
    const reader = sqlite(dir);
    await storeConnection(reader);
    await looping(1).run("From another handle.", { store: sqlite(dir) });
    await unwrapped(exporter(reader, c.url));
    expect(accepted(c).map((s) => s.name)).toContain("invoke_agent agent");
  });
});
