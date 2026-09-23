import { describe, expect, test } from "bun:test";
import {
  fencedFetch,
  loadedTools,
  memoryContext,
  parseRender,
  rejectionFor,
  StaleEpochError,
  staleEpoch,
} from "../../src/adapter";
import { Session } from "../../src/loop/session";
import { harness } from "../loop/harness";
import { ROOT, unwrap } from "../store/helpers";

const encoder = new TextEncoder();

describe("Session.modelContext: bound to the writer that made it", () => {
  test("after a takeover the old writer's context fails its fence; the new one's passes", async () => {
    const h = harness([], [], []);
    const old = unwrap(h.store.acquire(ROOT, "holder-a"));
    const stale = new Session(old, h.artifacts, h.config()).modelContext();
    expect(stale.branchId).toBe(ROOT);
    expect((await stale.fence()).ok).toBe(true);
    h.clock.now += 31_000;
    const fresh = unwrap(h.store.acquire(ROOT, "holder-b"));
    const live = new Session(fresh, h.artifacts, h.config()).modelContext();
    expect(live.epoch).toBe(stale.epoch + 1);
    const fenced = await stale.fence();
    expect(fenced.ok ? "ok" : fenced.error.code).toBe("stale_epoch");
    expect((await live.fence()).ok).toBe(true);
  });

  test("read verifies sha256 and length; put is readable back", async () => {
    const h = harness([], [], []);
    const writer = unwrap(h.store.acquire(ROOT, "holder-a"));
    const ctx = new Session(writer, h.artifacts, h.config()).modelContext();
    const ref = await ctx.put(encoder.encode("{}"), "application/json");
    expect(unwrap(await ctx.read(ref))).toEqual(encoder.encode("{}"));
    const missing = await ctx.read({ ...ref, sha256: "0".repeat(64) });
    expect(missing.ok ? "ok" : missing.error.code).toBe("artifact_missing");
    const short = await ctx.read({ ...ref, bytes: 1 });
    expect(short.ok ? "ok" : short.error.code).toBe("artifact_corrupt");
  });
});

describe("fencedFetch", () => {
  test("a stale context never calls the inner fetch", async () => {
    let calls = 0;
    const fetch = fencedFetch(
      memoryContext(() => false),
      async () => {
        calls += 1;
        return new Response("");
      },
    );
    const thrown = await fetch("https://x").catch((e: unknown) => e);
    expect(thrown).toBeInstanceOf(StaleEpochError);
    expect(calls).toBe(0);
    const wrapped = new Error("Connection error.", { cause: thrown });
    expect<unknown>(staleEpoch(wrapped)).toBe(thrown);
    expect(staleEpoch(new Error("other"))).toBeUndefined();
  });
});

describe("rejectionFor", () => {
  const h = (init: Record<string, string>) => new Headers(init);
  const k = { promptTooLong: false, retryable: true };
  test.each([
    [
      429,
      h({ "retry-after": "2" }),
      k,
      { reason: "rate_limited", retry_after_ms: 2000 },
    ],
    [
      429,
      h({ "retry-after-ms": "150.5" }),
      k,
      { reason: "rate_limited", retry_after_ms: 151 },
    ],
    [429, h({}), { ...k, retryable: false }, { reason: "provider_error" }],
    [529, h({}), k, { reason: "overloaded" }],
    [503, h({}), k, { reason: "overloaded" }],
    [502, h({}), k, { reason: "server_error" }],
    [400, h({}), { ...k, promptTooLong: true }, { reason: "prompt_too_long" }],
    [404, h({}), k, { reason: "provider_error" }],
  ] as const)("%i", (status, headers, kind, expected) => {
    expect(rejectionFor(status, headers, kind, 0)).toEqual({
      kind: "rejected",
      http_status: status,
      ...expected,
    });
  });

  test("an HTTP-date retry-after is measured from now", () => {
    const now = Date.parse("2026-09-23T00:00:00Z");
    expect(
      rejectionFor(
        429,
        h({ "retry-after": "Wed, 23 Sep 2026 00:00:05 GMT" }),
        k,
        now,
      ),
    ).toEqual({
      kind: "rejected",
      http_status: 429,
      reason: "rate_limited",
      retry_after_ms: 5000,
    });
  });
});

describe("parseRender", () => {
  const head = {
    adapter: { name: "a", version: "1", settings: {} },
    model: { provider: "p", name: "m" },
    params: {},
    system: "",
    tools: [
      { name: "a", description: "", input_schema: {} },
      { name: "d", description: "", deferred: true },
    ],
  };
  const body = (...lines: unknown[]) =>
    encoder.encode(lines.map((l) => `${JSON.stringify(l)}\n`).join(""));

  test("the latest tools line wins; deferred stubs are not offered", () => {
    const one = parseRender(body(head));
    expect(loadedTools(one).map((t) => t.name)).toEqual(["a"]);
    const two = parseRender(
      body(head, {
        role: "tools",
        tools: [{ name: "d", description: "", input_schema: {} }],
      }),
    );
    expect(loadedTools(two).map((t) => t.name)).toEqual(["d"]);
  });

  test("a line that isn't Render v1 is a bug: it throws", () => {
    expect(() =>
      parseRender(body(head, { role: "system", text: "x" })),
    ).toThrow();
    expect(() => parseRender(body({ ...head, extra: 1 }))).toThrow();
  });
});
