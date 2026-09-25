import { describe, expect, test } from "bun:test";
import { scriptedModel } from "../../src";
import { credential } from "../../src/agent/secret";
import type { LoopExtension } from "../../src/hooks/types";
import type { KnownEvent, Policy } from "../../src/log";
import { type LoopConfig, resume } from "../../src/loop";
import { CONTEXT_DEFAULTS, RETRY_DEFAULTS } from "../../src/loop/policy";
import type { Model } from "../../src/model";
import type { Writer } from "../../src/store";
import { ROOT, unwrap } from "../store/helpers";
import { after, cancel, cancelAt } from "./cancel-kit";
import { events, type Harness, harness, userInput } from "./harness";
import {
  asked,
  crashed,
  fail,
  outcome,
  reply,
  resumeOnce,
  SUMMARY_TEXT,
  side,
  sides,
} from "./manual-kit";

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
  config:
    | Partial<LoopConfig>
    | ((writer: () => Writer) => Partial<LoopConfig>) = {},
): Promise<readonly KnownEvent[]> {
  const first = unwrap(await h.store.acquire(ROOT, "first"));
  await resume(first, h.artifacts, h.config(), { input: userInput("first") });
  await first.release();
  const writer = unwrap(await h.store.acquire(ROOT, "second"));
  // One model for the whole run: it counts its sends.
  const second = model(() => writer);
  const extra = typeof config === "function" ? config(() => writer) : config;
  await resume(
    writer,
    h.artifacts,
    h.config({ models: () => second, ...extra }),
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
    const h = await harness(
      [],
      [],
      [say("one", 5_000)],
      undefined,
      policy(1_000),
    );
    const log = await secondTurn(h, (w) =>
      cancelAt([fail("prompt_too_long", 400), say("summary", 10)], 1, w),
    );
    endsCancelled(log);
  });

  test("a requested compaction's fallback: answered failed, and the turn ends cancelled", async () => {
    const h = await asked([fail("prompt_too_long", 400), reply(SUMMARY_TEXT)]);
    h.clock.now += 60_000;
    const writer = unwrap(await h.store.acquire(ROOT, "owner"));
    const model = cancelAt(
      [fail("prompt_too_long", 400), reply(SUMMARY_TEXT)],
      1,
      () => writer,
    );
    await resume(writer, h.artifacts, h.config({ models: () => model }));
    const log = events(writer);
    endsCancelled(log);
    // No clearing after the barrier, and the fallback's own reason.
    const barrier = log.findIndex((e) => e.type === "cancel_requested");
    expect(log.slice(barrier).some((e) => e.type === "context_edited")).toBe(
      false,
    );
    expect(log.find((e) => e.type === "compaction_failed")?.data).toMatchObject(
      {
        reason: "prompt_too_long",
      },
    );
  });
});

describe("a reactive compaction after prompt_too_long (L5)", () => {
  test("its leaked summary with a cancel pending ends the turn cancelled, not context_exhausted", async () => {
    const key = credential("fake", "apiKey", "sk-l09-l5-1a2b", "U")();
    const h = await harness(
      [],
      [],
      [say("one", 10)],
      undefined,
      policy(10_000_000),
    );
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
    const h = await harness([], [], [say("one", 10)]);
    const log = await secondTurn(h, (w) => cancelAt([say("two", 10)], 1, w), {
      onEvent: async (e: KnownEvent) => {
        if (e.type !== "cancelled") return;
        h.clock.now += 60_000;
        await unwrap(await h.store.acquire(ROOT, "usurper")).release();
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
    const h = await harness([], [], [say("one", 10)]);
    await secondTurn(h, (w) => cancelAt([say("x", 10)], 1, w, key), {
      onEvent: async (e: KnownEvent) => {
        if (e.type !== "model_attempt_abandoned") return;
        h.clock.now += 60_000;
        await unwrap(await h.store.acquire(ROOT, "usurper")).release();
      },
    });
    h.clock.now += 60_000;
    const older = unwrap(await h.store.acquire(ROOT, "older"));
    const barrier = events(older).findLast(
      (e) => e.type === "cancel_requested",
    );
    if (barrier === undefined) throw new Error("the cancel is recorded");
    unwrap(
      await older.append([
        {
          type: "cancelled",
          type_version: 1,
          critical: true,
          actor: { kind: "host" },
          data: { request_event_id: barrier.event_id },
        },
      ]),
    );
    await older.release();
    h.clock.now += 60_000;
    const next = unwrap(await h.store.acquire(ROOT, "next"));
    await resume(next, h.artifacts, h.config());
    const log = events(next);
    expect(log.slice(-2).map((e) => e.type)).toEqual([
      "cancelled",
      "turn_completed",
    ]);
    expect(log.at(-1)?.data).toEqual({ reason: "cancelled" });
  });
});

/** before_compact asks for a cancel, then lets the compaction go ahead. */
function cancelsThenProceeds(writer: () => Writer): LoopExtension {
  return {
    name: "ops",
    timeoutMs: 1_000,
    hooks: {
      before_compact: async () => {
        unwrap(await writer().append([cancel]));
        return { decision: "proceed" };
      },
    },
  };
}

/** Appends the cancel as the side request's prompt_too_long is recorded: the fallback is next. */
function cancelAtFallback(writer: () => Writer): Partial<LoopConfig> {
  return {
    onEvent: async (e: KnownEvent) => {
      if (
        e.type === "model_attempt_abandoned" &&
        e.data.reason === "prompt_too_long"
      )
        unwrap(await writer().append([cancel]));
    },
  };
}

function nothingSentAfterTheBarrier(log: readonly KnownEvent[]): void {
  const barrier = log.findIndex((e) => e.type === "cancel_requested");
  expect(barrier).toBeGreaterThan(-1);
  const rest = log.slice(barrier);
  expect(rest.some((e) => e.type === "model_request")).toBe(false);
  expect(rest.some((e) => e.type === "compaction_failed")).toBe(true);
  expect(log.at(-1)?.data).toEqual({ reason: "cancelled" });
}

describe("the barrier holds at the send, whatever came before it", () => {
  test("before_compact cancels then proceeds: an automatic compaction sends nothing", async () => {
    const h = await harness(
      [],
      [],
      [say("one", 5_000)],
      undefined,
      policy(1_000),
    );
    const log = await secondTurn(
      h,
      () => scriptedModel({ responses: [say("summary", 10), say("two", 10)] }),
      (w) => ({ extensions: [cancelsThenProceeds(w)] }),
    );
    nothingSentAfterTheBarrier(log);
  });

  test("before_compact cancels then proceeds: a requested compaction sends nothing", async () => {
    const h = await asked([reply(SUMMARY_TEXT)]);
    h.clock.now += 60_000;
    const writer = unwrap(await h.store.acquire(ROOT, "owner"));
    await resume(
      writer,
      h.artifacts,
      h.config({ extensions: [cancelsThenProceeds(() => writer)] }),
    );
    nothingSentAfterTheBarrier(events(writer));
  });

  test("a cancel as the fallback's results are cleared: an automatic compaction sends nothing more", async () => {
    const h = await harness(
      [],
      [],
      [say("one", 5_000)],
      undefined,
      policy(1_000),
    );
    const log = await secondTurn(
      h,
      () =>
        scriptedModel({
          responses: [fail("prompt_too_long", 400), say("summary", 10)],
        }),
      cancelAtFallback,
    );
    nothingSentAfterTheBarrier(log);
  });

  test("a cancel as the fallback's results are cleared: a requested compaction sends nothing more", async () => {
    const h = await asked([fail("prompt_too_long", 400), reply(SUMMARY_TEXT)]);
    h.clock.now += 60_000;
    const writer = unwrap(await h.store.acquire(ROOT, "owner"));
    await resume(writer, h.artifacts, h.config(cancelAtFallback(() => writer)));
    nothingSentAfterTheBarrier(events(writer));
  });
});

describe("L5 with its compaction already spent (TS)", () => {
  test("a cancel during the rejected turn request ends the turn cancelled, not context_exhausted", async () => {
    const h = await harness(
      [],
      [],
      [say("one", 5_000)],
      undefined,
      policy(1_000),
    );
    // The threshold compaction fails (empty summary), then the turn request is rejected.
    const log = await secondTurn(h, (w) =>
      cancelAt(
        [say("", 10), fail("prompt_too_long", 400), say("never", 10)],
        2,
        w,
      ),
    );
    const barrier = log.findIndex((e) => e.type === "cancel_requested");
    expect(
      log
        .slice(barrier)
        .some(
          (e) =>
            e.type === "turn_completed" &&
            e.data.reason === "context_exhausted",
        ),
    ).toBe(false);
    expect(log.at(-1)?.data).toEqual({ reason: "cancelled" });
  });
});

const retries = (max_retries: number): Policy => ({
  retry: { ...RETRY_DEFAULTS, max_retries },
});

describe("a rejected turn request with a cancel pending (the end-of-turn barrier)", () => {
  test("rate_limited past max_retries: the turn ends cancelled, not model_unavailable", async () => {
    const h = await harness([], [], [say("one", 10)], undefined, retries(0));
    const log = await secondTurn(h, (w) =>
      cancelAt([fail("rate_limited", 429), say("never", 10)], 1, w),
    );
    const barrier = log.findIndex((e) => e.type === "cancel_requested");
    expect(log.slice(barrier).map((e) => e.type)).toEqual([
      "cancel_requested",
      "model_attempt_abandoned",
      "cancelled",
      "turn_completed",
    ]);
    expect(log.at(-1)?.data).toEqual({ reason: "cancelled" });
  });

  test("rate_limited with retries left: no retry is scheduled after the barrier", async () => {
    const h = await harness([], [], [say("one", 10)], undefined, retries(3));
    const log = await secondTurn(h, (w) =>
      cancelAt([fail("rate_limited", 429), say("never", 10)], 1, w),
    );
    const barrier = log.findIndex((e) => e.type === "cancel_requested");
    expect(log.slice(barrier).some((e) => e.type === "retry_scheduled")).toBe(
      false,
    );
    expect(log.at(-1)?.data).toEqual({ reason: "cancelled" });
  });
});

describe("recovery of a requested compaction with a cancel pending", () => {
  test("a crashed side request is answered failed, never re-sent later", async () => {
    const h = await asked([reply(SUMMARY_TEXT), reply("A.")]);
    await crashed(h, async (log) => [await side(h, log, 1)]);
    await crashed(h, () => [cancel]);
    const log = await resumeOnce(h);
    expect(sides(log)).toHaveLength(0);
    expect(outcome(log)).toMatchObject({ type: "compaction_failed" });
    expect(log.at(-1)?.data).toEqual({ reason: "cancelled" });
  });
});
