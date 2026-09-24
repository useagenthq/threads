import { afterEach, describe, expect, test } from "bun:test";
import { mkdtempSync, readdirSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { z } from "zod";
import { agent, scriptedModel, sqlite, tool } from "../../src";
import { canonicalize } from "../../src/log";
import { eventsOf, requestsOf, say, startedOf, use } from "./tool-search-kit";

// The size budget of deferral (spec/schema/README.md, "Deferred tools and tool_search"): loads
// cost O(names) in the log, requests grow by the loaded specs only, and a deferred spec is
// stored once, shared by every thread of the agent.

const TOOLS = 500;
/** A ~1.5 KB schema: 30 described string properties. */
const input = z.object(
  Object.fromEntries(
    Array.from({ length: 30 }, (_, i) => [
      `field_${i}`,
      z.string().describe(`Field number ${i} of this rather large tool input.`),
    ]),
  ),
);
const many = Array.from({ length: TOOLS }, (_, i) =>
  tool({
    name: `bulk_${String(i).padStart(3, "0")}`,
    description: `Bulk operation ${i}.`,
    input,
    runs: "host",
    effect: "read_only",
    defer: true,
    execute: async () => "ok",
  }),
);

const dirs: string[] = [];
afterEach(() => {
  for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
});

function files(root: string): readonly string[] {
  return readdirSync(root, { recursive: true, encoding: "utf8" }).filter((f) =>
    statSync(join(root, f)).isFile(),
  );
}

const loads = Array.from({ length: 10 }, (_, i) => ({
  query: [0, 1, 2]
    .map((k) => `bulk_${String(i * 3 + k).padStart(3, "0")}`)
    .join(", "),
}));

describe("deferral at scale", () => {
  test("500 deferred tools, 10 loads of 3: small loads, bounded request growth, shared specs", async () => {
    const dir = mkdtempSync(join(tmpdir(), "threads-defer-"));
    dirs.push(dir);
    const store = sqlite(dir);
    const responses = [
      ...loads.map((q, i) => use("tool_search", q, `c${i}`)),
      say("done"),
    ];
    const bot = () =>
      agent({ model: scriptedModel({ responses }), tools: many });
    const result = await bot().run("Load them.", { store });
    expect(result.status).toBe("completed");
    const events = await eventsOf(store, result.thread);
    const started = startedOf(events);
    expect(
      started.data.tools.every(
        (t) => t.defer_loading !== true || t.input_schema === undefined,
      ),
    ).toBe(true);
    const loadBytes = events
      .filter((e) => e.type === "tools_loaded")
      .map((e) => {
        const text = canonicalize(JSON.parse(JSON.stringify(e)));
        return text.ok ? text.value.length : Number.POSITIVE_INFINITY;
      });
    expect(loadBytes).toHaveLength(10);
    expect(loadBytes.reduce((a, b) => a + b, 0)).toBeLessThan(16 * 1024);
    const requests = await requestsOf(store, events);
    const spec = new TextEncoder().encode(
      JSON.stringify(many[0]?.spec()),
    ).length;
    for (const [i, bytes] of requests.entries()) {
      const previous = requests[i - 1];
      if (previous !== undefined)
        expect(bytes.length - previous.length).toBeLessThanOrEqual(
          3 * spec + 1024,
        );
    }
    const artifacts = join(dir, "artifacts");
    const specRefs = new Set(
      started.data.tools.flatMap((t) =>
        t.spec_ref === undefined ? [] : [t.spec_ref.sha256],
      ),
    );
    expect(specRefs.size).toBe(TOOLS);
    const before = files(artifacts);
    const second = await bot().run("Again.", { store });
    const again = startedOf(await eventsOf(store, second.thread));
    expect(again.data.tools.map((t) => t.spec_ref?.sha256)).toEqual(
      started.data.tools.map((t) => t.spec_ref?.sha256),
    );
    // The second thread's 500 puts refresh existing files: no new spec artifact file.
    const after = files(artifacts);
    const specFiles = (list: readonly string[]) =>
      list.filter((f) => specRefs.has(f.split("/").at(-1) ?? ""));
    expect(specFiles(after)).toEqual(specFiles(before));
    for (const f of specFiles(after))
      expect(statSync(join(artifacts, f)).nlink).toBe(1);
  }, 60_000);
});
