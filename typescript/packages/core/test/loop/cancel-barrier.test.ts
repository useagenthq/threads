import { describe, expect, test } from "bun:test";
import { credential } from "../../src/agent/secret";
import type { KnownEvent, Policy } from "../../src/log";
import { type LoopConfig, resume } from "../../src/loop";
import { CONTEXT_DEFAULTS } from "../../src/loop/policy";
import type { Model } from "../../src/model";
import type { Writer } from "../../src/store";
import { ROOT, unwrap } from "../store/helpers";
import { after, cancelAt } from "./cancel-kit";
import { events, type Harness, harness, userInput } from "./harness";
import { asked, fail, reply, SUMMARY_TEXT } from "./manual-kit";

// Nothing is sent after a cancel barrier, and the turn it asks to stop ends cancelled: through a
// compaction's fallback, a reactive compaction after prompt_too_long, and a lease lost right
// after the cancellation is recorded.

const say = (text: string, input: number) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage: { input_tokens: input, output_tokens: 5 },
});

function policy(trigger: number): Policy {
  return {
    context: {
      ...CONTEXT_DEFAULTS,
      compact: {
        ...CONTEXT_DEFAULTS.compact,
        keep_tail: { tokens: 1 },
        trigger: { tokens: trigger },
      },
    },
  };
}

const TAIL = [
  "cancel_requested",
  "model_attempt_abandoned",
  "compaction_failed",
  "cancelled",
  "turn_completed",
];

/** A first turn, then a second run by `model` (built with the second run's writer). */
async function secondTurn(
  h: Harness,
  model: (writer: () => Writer) => Model,
  config: Partial<LoopConfig> = {},
): Promise<readonly KnownEvent[]> {
  const first = unwrap(h.store.acquire(ROOT, "first"));
  await resume(first, h.artifacts, h.config(), { input: userInput("first") });
  first.release();
  const writer = unwrap(h.store.acquire(ROOT, "second"));
  // One model for the whole run: it counts its sends.
  const second = model(() => writer);
  await resume(
    writer,
    h.artifacts,
    h.config({ models: () => second, ...config }),
    { input: userInput("second") },
  );
  return events(writer);
}

function endsCancelled(log: readonly KnownEvent[]): void {
  expect(after(log, "cancel_requested")).toEqual(TAIL);
  expect(log.at(-1)?.data).toEqual({ reason: "cancelled" });
}

describe("no side request is sent after the barrier", () => {
  test("an automatic compaction's fallback: failed, and the turn ends cancelled", async () => {
    const h = harness([], [], [say("one", 5_000)], undefined, policy(1_000));
    const log = await secondTurn(h, (w) =>
      cancelAt([fail("prompt_too_long", 400), say("summary", 10)], 1, w),
    );
    endsCancelled(log);
  });

  test("a requested compaction's fallback: answered failed, and the turn ends cancelled", async () => {
    const h = asked([fail("prompt_too_long", 400), reply(SUMMARY_TEXT)]);
    h.clock.now += 60_000;
    const writer = unwrap(h.store.acquire(ROOT, "owner"));
    const model = cancelAt(
      [fail("prompt_too_long", 400), reply(SUMMARY_TEXT)],
      1,
      () => writer,
    );
    await resume(writer, h.artifacts, h.config({ models: () => model }));
    endsCancelled(events(writer));
  });
});

describe("a reactive compaction after prompt_too_long (L5)", () => {
  test("its leaked summary with a cancel pending ends the turn cancelled, not context_exhausted", async () => {
    const key = credential("fake", "apiKey", "sk-l09-l5-1a2b", "U")();
    const h = harness([], [], [say("one", 10)], undefined, policy(10_000_000));
    const log = await secondTurn(h, (w) =>
      cancelAt(
        [fail("prompt_too_long", 400), say("never recorded", 10)],
        2,
        w,
        key,
      ),
    );
    endsCancelled(log);
  });
});

describe("a lease lost right after the cancellation is recorded", () => {
  test("the turn is already closed cancelled: nothing is torn", async () => {
    const h = harness([], [], [say("one", 10)]);
    const log = await secondTurn(h, (w) => cancelAt([say("two", 10)], 1, w), {
      onEvent: (e: KnownEvent) => {
        if (e.type !== "cancelled") return;
        h.clock.now += 60_000;
        unwrap(h.store.acquire(ROOT, "usurper")).release();
      },
    });
    expect(log.slice(-2).map((e) => e.type)).toEqual([
      "cancelled",
      "turn_completed",
    ]);
    expect(log.at(-1)?.data).toEqual({ reason: "cancelled" });
  });

  test("a log torn after cancelled (an older writer) is recovered as cancelled, not interrupted", async () => {
    const key = credential("fake", "apiKey", "sk-l09-torn-3c4d", "U")();
    const h = harness([], [], [say("one", 10)]);
    await secondTurn(h, (w) => cancelAt([say("x", 10)], 1, w, key), {
      onEvent: (e: KnownEvent) => {
        if (e.type !== "model_attempt_abandoned") return;
        h.clock.now += 60_000;
        unwrap(h.store.acquire(ROOT, "usurper")).release();
      },
    });
    h.clock.now += 60_000;
    const older = unwrap(h.store.acquire(ROOT, "older"));
    const barrier = events(older).findLast(
      (e) => e.type === "cancel_requested",
    );
    if (barrier === undefined) throw new Error("the cancel is recorded");
    unwrap(
      older.append([
        {
          type: "cancelled",
          type_version: 1,
          critical: true,
          actor: { kind: "host" },
          data: { request_event_id: barrier.event_id },
        },
      ]),
    );
    older.release();
    h.clock.now += 60_000;
    const next = unwrap(h.store.acquire(ROOT, "next"));
    await resume(next, h.artifacts, h.config());
    const log = events(next);
    expect(log.slice(-2).map((e) => e.type)).toEqual([
      "cancelled",
      "turn_completed",
    ]);
    expect(log.at(-1)?.data).toEqual({ reason: "cancelled" });
  });
});
