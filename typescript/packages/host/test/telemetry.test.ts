import { afterEach, describe, expect, spyOn, test } from "bun:test";
import {
  type Exporter,
  type SyncError,
  type SyncReport,
  sqlite,
} from "@threads/core";
import { BranchId, err, ok, type Result, ThreadId } from "@threads/core/host";
import { Telemetry } from "../src/telemetry";

// The host's telemetry loop with a scripted exporter: what it logs, once per streak, and how it
// backs off a failing collector. host({telemetry}) itself is in packages/otel/test/host.test.ts.

const FAST = { everyMs: 5, maxWaitMs: 40, lastSyncMs: 200 };
const BRANCH = BranchId.parse("0192b000-0000-7000-8000-000000000001");
const THREAD = ThreadId.parse("0192a000-0000-7000-8000-000000000001");

type Answer = Result<SyncReport, SyncError>;

function scripted(answers: Answer[]): {
  readonly exporter: Exporter;
  readonly calls: () => number;
} {
  let n = 0;
  return {
    exporter: {
      sync: async () => {
        n += 1;
        return (
          answers.shift() ??
          ok({ spans: 0, possiblyLostEvents: 0, skipped: [] })
        );
      },
    },
    calls: () => n,
  };
}

const skipped = (head: number): Answer =>
  ok({
    spans: 0,
    possiblyLostEvents: 0,
    skipped: [
      {
        branch_id: BRANCH,
        thread_id: THREAD,
        code: "log_corrupt",
        head_seq: head,
      },
    ],
  });

const down: Answer = err({
  code: "collector_unavailable",
  status: 503,
  message:
    "the collector at http://127.0.0.1:1/v1/traces is unavailable: HTTP 503",
});

async function until(done: () => boolean): Promise<void> {
  for (let i = 0; i < 400 && !done(); i++) await Bun.sleep(5);
  if (!done()) throw new Error("timed out");
}

let lines: string[] = [];
let restore = (): void => {};
function capture(): void {
  lines = [];
  const spy = spyOn(console, "error").mockImplementation(
    (...args: unknown[]) => {
      lines.push(String(args[0]));
    },
  );
  restore = () => spy.mockRestore();
}
afterEach(() => {
  restore();
});

describe("host telemetry", () => {
  test("a skipped branch is logged once per streak, and again after its head moves", async () => {
    capture();
    const s = scripted([
      skipped(4),
      skipped(4),
      skipped(4),
      skipped(4),
      skipped(4),
      skipped(6),
    ]);
    const t = new Telemetry(s.exporter, sqlite(":memory:"), FAST);
    t.start();
    await until(() => s.calls() >= 7);
    await t.stop();
    expect(lines.filter((l) => l.includes(BRANCH))).toHaveLength(2);
  });

  test("a failing collector is logged once per streak and retried with back-off", async () => {
    capture();
    const s = scripted([
      down,
      down,
      down,
      ok({ spans: 1, possiblyLostEvents: 0, skipped: [] }),
      down,
    ]);
    const t = new Telemetry(s.exporter, sqlite(":memory:"), FAST);
    t.start();
    await until(() => s.calls() >= 6);
    await t.stop();
    expect(
      lines.filter((l) => l.includes("collector_unavailable")),
    ).toHaveLength(2);
  });

  test("stop() runs one last sync, bounded", async () => {
    capture();
    const s = scripted([]);
    const t = new Telemetry(s.exporter, sqlite(":memory:"), FAST);
    await t.stop();
    expect(s.calls()).toBe(1);
    const hung = new Telemetry(
      { sync: () => new Promise(() => {}) },
      sqlite(":memory:"),
      FAST,
    );
    const started = Date.now();
    await hung.stop();
    expect(Date.now() - started).toBeLessThan(2_000);
  });
});
