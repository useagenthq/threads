import { afterEach, describe, expect, test } from "bun:test";
import { sqlite } from "@threads/core";
import { knownEvents, openStore } from "@threads/core/host";
import { host } from "@threads/host";
import { Telemetry } from "../../host/src/telemetry";
import { otel } from "../src";
import { accepted, type Collector, collector } from "./collector";
import { cursors, looping } from "./kit";

// host({telemetry: otel()}): the host's store is exported on its own timer and once more on
// stop(); a collector that never answers holds up no run.

let c: Collector;
afterEach(async () => {
  await c.stop();
});

async function until(done: () => boolean, ms = 5_000): Promise<void> {
  const end = Date.now() + ms;
  while (!done() && Date.now() < end) await Bun.sleep(20);
}

/** The event types a run on a fresh store writes, with the host's telemetry or without. */
async function steps(telemetry: boolean): Promise<readonly string[]> {
  const store = sqlite(":memory:");
  const bot = looping(3);
  const served = telemetry
    ? host({ store, agents: { bot }, telemetry: otel({ endpoint: c.url }) })
    : host({ store, agents: { bot } });
  await served.ready();
  await Bun.sleep(1_200);
  const result = await bot.run("Hi.", { store });
  const started = Date.now();
  await served.stop();
  expect(Date.now() - started).toBeLessThan(7_000);
  const { log } = await openStore(store);
  const read = log.read(result.thread.branch);
  return read.ok ? knownEvents(read.value).map((e) => e.type) : [];
}

describe("host({telemetry})", () => {
  test("exports the host's store on the tick", async () => {
    c = collector();
    const store = sqlite(":memory:");
    const bot = looping(2);
    const served = host({
      store,
      agents: { bot },
      telemetry: otel({ endpoint: c.url }),
    });
    await served.ready();
    try {
      await bot.run("Hi.", { store });
      await until(() =>
        accepted(c).some((s) => s.name.startsWith("invoke_agent")),
      );
      expect(accepted(c).map((s) => s.name)).toContain("invoke_agent agent");
    } finally {
      await served.stop();
    }
  });

  test("stop() runs one last sync", async () => {
    c = collector();
    const store = sqlite(":memory:");
    const bot = looping(1);
    const served = host({
      store,
      agents: { bot },
      telemetry: otel({ endpoint: c.url }),
    });
    await served.ready();
    await bot.run("Hi.", { store });
    await served.stop();
    expect(accepted(c).map((s) => s.name)).toContain("invoke_agent agent");
  });

  test("stop() aborts the POST in flight: nothing of the exporter runs after it returns", async () => {
    c = collector();
    c.answers = Array.from({ length: 10 }, () => "hang" as const);
    const store = sqlite(":memory:");
    await looping(1).run("Hi.", { store });
    const exporter = otel({ endpoint: c.url });
    const t = new Telemetry(exporter, store, {
      everyMs: 5,
      maxWaitMs: 40,
      lastSyncMs: 200,
    });
    t.start();
    await until(() => c.attempts.length > 0);
    const started = Date.now();
    await t.stop();
    expect(Date.now() - started).toBeLessThan(1_000);
    // Its queue is free at once: nothing is still waiting on the hung collector.
    const after = Date.now();
    const next = await exporter.sync();
    expect(Date.now() - after).toBeLessThan(500);
    expect(next).toMatchObject({
      ok: false,
      error: { code: "collector_unavailable" },
    });
    expect(await cursors(store)).toEqual({});
  });

  test("a collector that never answers holds up no run, and stop() is bounded", async () => {
    c = collector();
    c.answers = Array.from({ length: 50 }, () => "hang" as const);
    const without = await steps(false);
    expect(await steps(true)).toEqual(without);
    expect(without.at(-1)).toBe("turn_completed");
    expect(accepted(c)).toEqual([]);
  }, 20_000);
});
