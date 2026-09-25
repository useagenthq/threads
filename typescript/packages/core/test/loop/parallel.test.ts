import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { agent, scriptedModel, sqlite, tool } from "../../src";
import { openStore } from "../../src/agent/sqlite";
import type { LoopExtension } from "../../src/hooks/types";
import type { KnownEvent } from "../../src/log";
import type { ToolImpl, ToolRun } from "../../src/loop";
import type { Model } from "../../src/model";
import { markTestKit } from "../../src/model/guard";
import { knownEvents } from "../../src/reduce";
import { unwrap } from "../store/helpers";
import { events } from "./harness";
import {
  after,
  calls,
  done,
  FINAL,
  impl,
  read,
  results,
  run,
  write,
} from "./parallel-kit";

const RECORDED_ORDER = z
  .object({ recorded_order: z.array(z.string()) })
  .parse(
    JSON.parse(
      readFileSync(
        join(
          import.meta.dir,
          "../../../../../spec/conformance/vectors/tool-groups.json",
        ),
        "utf8",
      ),
    ),
  ).recorded_order;

// Parallel tool calls (spec/schema/README.md): concurrent read-only calls of one response run
// together, every other call runs alone, and results are recorded in call order.

/** Waits for `gate`, or gives up after 2 s: the calls then ran one after the other. */
async function meet(gate: Promise<void>): Promise<string> {
  const timeout = Promise.withResolvers<string>();
  const timer = setTimeout(() => timeout.resolve("ran alone"), 2_000);
  const met = await Promise.race([
    (async () => {
      await gate;
      return "ran together";
    })(),
    timeout.promise,
  ]);
  clearTimeout(timer);
  return met;
}

describe("tool-parallel-safe-reads (F1.1)", () => {
  test("two concurrent reads of one response run at the same time", async () => {
    const a = Promise.withResolvers<void>();
    const b = Promise.withResolvers<void>();
    // Each body waits on a latch only the other one releases.
    const lookup = (name: string, mine: Promise<void>, theirs: () => void) =>
      tool({
        name,
        description: `Look up ${name}.`,
        input: z.object({}),
        effect: "read_only",
        concurrent: true,
        execute: async () => {
          theirs();
          return `${name} ${await meet(mine)}`;
        },
      });
    const scripted = scriptedModel({
      responses: [calls("orders", "invoices"), FINAL],
    });
    const bodies: string[] = [];
    // The scripted model, recording the bytes of each request it is sent.
    const model: Model = {
      ...scripted,
      send: (request, ctx) => {
        bodies.push(new TextDecoder().decode(request.body));
        return scripted.send(request, ctx);
      },
    };
    markTestKit(model);
    const bot = agent({
      model,
      tools: [
        lookup("orders", a.promise, b.resolve),
        lookup("invoices", b.promise, a.resolve),
      ],
    });
    const result = await bot.run("go", { store: sqlite(":memory:") });
    expect(result.status).toBe("completed");
    const { log } = await openStore(result.thread.store);
    const all = knownEvents(unwrap(await log.read(result.thread.branch)));
    const shown = all.flatMap((e) =>
      e.type === "tool_result" ? [[e.data.call_id, e.data.preview]] : [],
    );
    expect(shown).toEqual([
      ["call_1", "orders ran together"],
      ["call_2", "invoices ran together"],
    ]);
    // Both results are in before the next request, and in its bytes.
    const types = all.map((e) => e.type);
    expect(types.lastIndexOf("tool_result")).toBeLessThan(
      types.lastIndexOf("model_request"),
    );
    const next = bodies.at(-1) ?? "";
    for (const shown of ["orders ran together", "invoices ran together"])
      expect(next.split(shown).length).toBe(2);
  });
});

describe("tool-exclusive-no-overlap (F1.2)", () => {
  const traced = (trace: string[]) => (name: string) => async () => {
    trace.push(`start ${name}`);
    await after(1);
    trace.push(`end ${name}`);
    return done(name);
  };

  test("a write runs alone between two concurrent reads", async () => {
    const trace: string[] = [];
    const body = traced(trace);
    let last = (): string | undefined => undefined;
    const send = body("send");
    const { log } = await run(
      [
        impl(read("read_a"), body("read_a")),
        impl(
          write("send"),
          async () => {
            trace.push(`begun ${last()}`);
            return send();
          },
          false,
        ),
        impl(read("read_b"), body("read_b")),
      ],
      [calls("read_a", "send", "read_b"), FINAL],
      (_h, writer) => {
        last = () => events(writer).at(-1)?.type;
        return {};
      },
    );
    expect(trace).toEqual([
      "start read_a",
      "end read_a",
      "begun effect_begin",
      "start send",
      "end send",
      "start read_b",
      "end read_b",
    ]);
    const types = log.map((e) => e.type);
    expect(types.indexOf("effect_begin")).toBeLessThan(
      types.indexOf("effect_commit"),
    );
    expect(results(log)).toEqual(["call_1", "call_2", "call_3"]);
  });

  test("two writes never overlap", async () => {
    const trace: string[] = [];
    const body = traced(trace);
    await run(
      [impl(write("send"), body("send"), false)],
      [calls("send", "send"), FINAL],
    );
    expect(trace).toEqual(["start send", "end send", "start send", "end send"]);
  });

  test("a read that isn't concurrent is a barrier", async () => {
    const trace: string[] = [];
    const body = traced(trace);
    await run(
      [
        impl(read("read_a"), body("read_a")),
        impl(read("plain"), body("plain"), false),
      ],
      [calls("read_a", "plain", "read_a"), FINAL],
    );
    expect(trace).toEqual([
      "start read_a",
      "end read_a",
      "start plain",
      "end plain",
      "start read_a",
      "end read_a",
    ]);
  });
});

describe("recorded order (replay)", () => {
  /** Three reads that finish in reverse order; the second brings an injection. */
  function reads(finished: string[], concurrent: boolean): readonly ToolImpl[] {
    const body =
      (name: string, ms: number, run: ToolRun) =>
      async (): Promise<ToolRun> => {
        await after(ms);
        finished.push(name);
        return run;
      };
    const noted: ToolRun = {
      kind: "done",
      output: "b",
      isError: false,
      inject: [
        {
          source: "memory",
          trust: "untrusted_reference",
          origin: { id: "m1" },
          text: "b was read",
        },
      ],
    };
    return [
      impl(read("a"), body("a", 30, done("a")), concurrent),
      impl(read("b"), body("b", 15, noted), concurrent),
      impl(read("c"), body("c", 1, done("c")), concurrent),
    ];
  }

  const observer: LoopExtension = {
    name: "audit",
    timeoutMs: 1_000,
    hooks: { after_tool: async () => ["seen"] },
  };

  /** The log without what differs between runs: event ids, times, epochs and hashes. */
  function normalized(log: readonly KnownEvent[]): string {
    const text = JSON.stringify(
      log.map((e) => ({ type: e.type, actor: e.actor, data: e.data })),
    );
    return log.reduce(
      (out, e, i) => out.replaceAll(e.event_id, `event_${i}`),
      text,
    );
  }

  test("a group writes the same events in the same order as a sequential run", async () => {
    const together: string[] = [];
    const alone: string[] = [];
    const script = [calls("a", "b", "c"), FINAL];
    const extensions = () => ({ extensions: [observer] });
    const parallel = await run(reads(together, true), script, extensions);
    const sequential = await run(reads(alone, false), script, extensions);
    expect(together).toEqual(["c", "b", "a"]);
    expect(alone).toEqual(["a", "b", "c"]);
    expect(normalized(parallel.log)).toBe(normalized(sequential.log));
    // The same sequence Python records (spec/conformance/vectors/tool-groups.json).
    const trace = parallel.log.flatMap((e) =>
      e.type === "tool_result"
        ? [`tool_result:${e.data.call_id}`]
        : e.type === "hook_decision"
          ? [`after_tool:${e.data.call_id}`]
          : e.type === "injected"
            ? ["injected"]
            : [],
    );
    expect(trace).toEqual(RECORDED_ORDER);
  });
});

describe("a window of 8", () => {
  test("at most 8 calls are started and unrecorded; call 9 waits for result 1", async () => {
    let outstanding = 0;
    let most = 0;
    let startedWhenFirstRecorded = 0;
    let started = 0;
    const body = (slow: boolean) => async () => {
      started += 1;
      outstanding += 1;
      most = Math.max(most, outstanding);
      await after(slow ? 30 : 1);
      return done("ok");
    };
    const names = Array.from({ length: 20 }, (_, i) => `r${i + 1}`);
    const { log } = await run(
      names.map((n) => impl(read(n), body(n === "r1"))),
      [calls(...names), FINAL],
      () => ({
        onEvent: (e) => {
          if (e.type !== "tool_result") return;
          outstanding -= 1;
          if (e.data.call_id === "call_1") startedWhenFirstRecorded = started;
        },
      }),
    );
    expect(most).toBe(8);
    expect(startedWhenFirstRecorded).toBe(8);
    expect(results(log)).toEqual(names.map((_, i) => `call_${i + 1}`));
  });
});
