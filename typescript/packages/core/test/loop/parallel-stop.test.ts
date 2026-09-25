import { describe, expect, test } from "bun:test";
import { resume } from "../../src/loop";
import { ROOT, unwrap } from "../store/helpers";
import { events } from "./harness";
import {
  calls,
  done,
  FINAL,
  impl,
  read,
  results,
  run,
  takeOver,
} from "./parallel-kit";

// Stopping inside a group: a cancel is a barrier after the started reads, a lost lease records
// nothing more and aborts the started bodies, and a new owner re-runs the unrecorded reads.

const names = (n: number): readonly string[] =>
  Array.from({ length: n }, (_, i) => `r${i + 1}`);

describe("cancel mid-group", () => {
  test("started reads finish and are recorded; queued ones never start", async () => {
    const gate = Promise.withResolvers<void>();
    const runs = new Map<string, number>();
    const all = names(10);
    let cancel = async (): Promise<unknown> => undefined;
    const { log, end } = await run(
      all.map((n) =>
        impl(read(n), async () => {
          runs.set(n, (runs.get(n) ?? 0) + 1);
          // The 8th start fills the window: cancel while all 8 wait on the gate.
          if (runs.size === 8) {
            await cancel();
            gate.resolve();
          }
          await gate.promise;
          return done(n);
        }),
      ),
      [calls(...all), FINAL],
      (_h, writer) => {
        cancel = async () =>
          unwrap(
            await writer.append([
              {
                type: "cancel_requested",
                type_version: 1,
                critical: true,
                actor: {
                  kind: "user",
                  principal: {
                    issuer: "api",
                    tenant: "acme",
                    subject: "alice",
                  },
                },
                data: { scope: "turn" },
              },
            ]),
          );
        return {};
      },
    );
    expect(end.kind).toBe("idle");
    expect([...runs.keys()]).toEqual(all.slice(0, 8));
    expect(results(log)).toEqual(all.map((_, i) => `call_${i + 1}`));
    const closed = log.flatMap((e) =>
      e.type === "tool_result" ? [[e.data.origin, e.data.preview]] : [],
    );
    expect(closed.slice(8)).toEqual([
      ["not_executed", "not executed: cancelled"],
      ["not_executed", "not executed: cancelled"],
    ]);
    const last = log.at(-1);
    expect(last?.type === "turn_completed" && last.data.reason).toBe(
      "cancelled",
    );
  });
});

describe("a lost lease inside a group", () => {
  test("the next body is never invoked, started bodies are aborted and awaited", async () => {
    const runs = new Map<string, number>();
    const caller = new AbortController();
    let sawAbort = false;
    let exited = false;
    const bodies = (takeover?: () => void) =>
      names(3).map((n) =>
        impl(read(n), async (ctx) => {
          runs.set(n, (runs.get(n) ?? 0) + 1);
          if (n !== "r1" || takeover === undefined) return done(n);
          takeover();
          const aborted = Promise.withResolvers<void>();
          ctx.signal.addEventListener("abort", () => aborted.resolve());
          await aborted.promise;
          sawAbort = ctx.signal.aborted;
          exited = true;
          return done(n);
        }),
      );
    let hold: (() => void) | undefined;
    const first = await run(
      bodies(() => hold?.()),
      [calls(...names(3)), FINAL],
      (h) => {
        hold = () => takeOver(h);
        return { signal: caller.signal };
      },
    );
    expect(first.end).toMatchObject({
      kind: "halted",
      halt: { code: "branch_busy" },
    });
    expect(runs.get("r2")).toBeUndefined();
    expect(sawAbort && exited).toBe(true);
    expect(caller.signal.aborted).toBe(false);
    expect(results(first.log)).toEqual([]);

    // The new owner runs the unrecorded reads in call order, once each.
    const usurper = unwrap(
      await first.h.store.acquire(ROOT, "usurper", 30_000),
    );
    runs.clear();
    const again = await resume(
      usurper,
      first.h.artifacts,
      first.h.config({
        tools: new Map(bodies().map((i) => [i.spec.name, i])),
      }),
    );
    expect(again.kind).toBe("idle");
    expect(results(events(usurper))).toEqual(["call_1", "call_2", "call_3"]);
    expect([...runs.values()]).toEqual([1, 1, 1]);
  });
});

describe("crash mid-drain", () => {
  test("after result 1 of 3, recovery re-runs calls 2 and 3 once each", async () => {
    const runs = new Map<string, number>();
    const bodies = names(3).map((n) =>
      impl(read(n), async () => {
        runs.set(n, (runs.get(n) ?? 0) + 1);
        return done(n);
      }),
    );
    const first = await run(bodies, [calls(...names(3)), FINAL], (h) => ({
      onEvent: async (e) => {
        if (e.type === "tool_result" && e.data.call_id === "call_1")
          await takeOver(h);
      },
    }));
    expect(first.end).toMatchObject({ kind: "halted" });
    expect(results(first.log)).toEqual(["call_1"]);
    const usurper = unwrap(
      await first.h.store.acquire(ROOT, "usurper", 30_000),
    );
    const again = await resume(
      usurper,
      first.h.artifacts,
      first.h.config({ tools: new Map(bodies.map((i) => [i.spec.name, i])) }),
    );
    expect(again.kind).toBe("idle");
    expect(results(events(usurper))).toEqual(["call_1", "call_2", "call_3"]);
    expect(Object.fromEntries(runs)).toEqual({ r1: 1, r2: 2, r3: 2 });
  });
});
