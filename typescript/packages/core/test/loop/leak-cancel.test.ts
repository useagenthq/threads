import { describe, expect, test } from "bun:test";
import { scriptedModel } from "../../src";
import { credential } from "../../src/agent/secret";
import type { KnownEvent, Policy } from "../../src/log";
import { type LoopConfig, resume } from "../../src/loop";
import { CONTEXT_DEFAULTS } from "../../src/loop/policy";
import type { Model } from "../../src/model";
import { markTestKit } from "../../src/model/guard";
import type { EventDraft, Writer } from "../../src/store";
import { ROOT, unwrap } from "../store/helpers";
import { events, type Harness, harness, userInput } from "./harness";
import { asked } from "./manual-kit";

// A cancel that lands while a response is refused as leaked (C5) is still processed: the leak
// records its abandonment (and a requested compaction's failure), and the cancellation step
// closes the turn as cancelled.

const encoder = new TextEncoder();

const cancel: EventDraft = {
  type: "cancel_requested",
  type_version: 1,
  critical: true,
  actor: {
    kind: "user",
    principal: { issuer: "api", tenant: "acme", subject: "operator" },
  },
  data: { scope: "turn" },
};

/** A test-kit model that asks for a cancel, then puts provider material holding `key`. */
function leakyAfterCancel(key: string, writer: () => Writer): Model {
  const model: Model = {
    info: scriptedModel({ responses: [] }).info,
    send: async function* (_request, context) {
      unwrap(writer().append([cancel]));
      await context.put(
        encoder.encode(JSON.stringify({ encrypted: key })),
        "application/json",
      );
      yield {
        kind: "done",
        stop_reason: "end_turn",
        usage: { input_tokens: 10, output_tokens: 2 },
      };
    },
  };
  markTestKit(model);
  return model;
}

const after = (log: readonly KnownEvent[], type: KnownEvent["type"]) =>
  log.slice(log.findIndex((e) => e.type === type)).map((e) => e.type);

describe("a cancel during a leaked response", () => {
  test("a turn request: abandoned, then the turn ends cancelled", async () => {
    const key = credential("fake", "apiKey", "sk-l09-cancel-1a2b", "U")();
    const h = harness([], [], []);
    const writer = unwrap(h.store.acquire(ROOT, "owner", 30_000));
    await resume(
      writer,
      h.artifacts,
      h.config({ models: () => leakyAfterCancel(key, () => writer) }),
      { input: userInput("go") },
    );
    const log = events(writer);
    expect(after(log, "cancel_requested")).toEqual([
      "cancel_requested",
      "model_attempt_abandoned",
      "cancelled",
      "turn_completed",
    ]);
    expect(log.at(-1)?.data).toEqual({ reason: "cancelled" });
  });

  test("a requested compaction: abandoned and failed, then the turn ends cancelled", async () => {
    const key = credential("fake", "apiKey", "sk-l09-cancel-3c4d", "U")();
    const h = asked([]);
    h.clock.now += 60_000;
    const writer = unwrap(h.store.acquire(ROOT, "owner"));
    await resume(
      writer,
      h.artifacts,
      h.config({ models: () => leakyAfterCancel(key, () => writer) }),
    );
    const log = events(writer);
    expect(after(log, "cancel_requested")).toEqual([
      "cancel_requested",
      "model_attempt_abandoned",
      "compaction_failed",
      "cancelled",
      "turn_completed",
    ]);
    expect(log.at(-1)?.data).toEqual({ reason: "cancelled" });
  });
});

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

/** A first turn that fills the context, then a second whose summary leaks after a cancel. */
async function secondTurn(
  h: Harness,
  key: string,
  config: Partial<LoopConfig> = {},
): Promise<{ readonly writer: Writer; readonly log: readonly KnownEvent[] }> {
  const first = unwrap(h.store.acquire(ROOT, "first"));
  await resume(first, h.artifacts, h.config(), { input: userInput("first") });
  first.release();
  const writer = unwrap(h.store.acquire(ROOT, "second"));
  await resume(
    writer,
    h.artifacts,
    h.config({ models: () => leakyAfterCancel(key, () => writer), ...config }),
    { input: userInput("second") },
  );
  return { writer, log: events(writer) };
}

describe("a cancel during a leaked automatic compaction", () => {
  test("threshold: the compaction fails, nothing more is sent, and the turn ends cancelled", async () => {
    const key = credential("fake", "apiKey", "sk-l09-cancel-5e6f", "U")();
    const h = harness([], [], [say("one", 5_000)], undefined, policy(1_000));
    const { log } = await secondTurn(h, key);
    expect(after(log, "cancel_requested")).toEqual([
      "cancel_requested",
      "model_attempt_abandoned",
      "compaction_failed",
      "cancelled",
      "turn_completed",
    ]);
    expect(log.at(-1)?.data).toEqual({ reason: "cancelled" });
  });

  test("reactive preflight: the compaction fails, and the turn ends cancelled, not context_exhausted", async () => {
    const key = credential("fake", "apiKey", "sk-l09-cancel-7a8b", "U")();
    const h = harness(
      [],
      [],
      [say("one", 185_000)],
      undefined,
      policy(10_000_000),
    );
    const { log } = await secondTurn(h, key);
    expect(after(log, "cancel_requested")).toEqual([
      "cancel_requested",
      "model_attempt_abandoned",
      "compaction_failed",
      "cancelled",
      "turn_completed",
    ]);
    expect(log.at(-1)?.data).toEqual({ reason: "cancelled" });
  });
});

describe("a crash right after the leak, with the cancel pending", () => {
  /** The owner loses its lease as `type` is recorded; the next owner recovers the turn. */
  async function crashAt(
    type: KnownEvent["type"],
  ): Promise<readonly KnownEvent[]> {
    const key = credential("fake", "apiKey", "sk-l09-crash-9c0d", "U")();
    const h = harness([], [], [say("one", 5_000)], undefined, policy(1_000));
    await secondTurn(h, key, {
      onEvent: (e: KnownEvent) => {
        if (e.type !== type) return;
        h.clock.now += 60_000;
        unwrap(h.store.acquire(ROOT, "usurper")).release();
      },
    });
    h.clock.now += 60_000;
    const next = unwrap(h.store.acquire(ROOT, "next"));
    await resume(next, h.artifacts, h.config());
    return events(next);
  }

  test("after the abandonment: recovery carries the cancel out", async () => {
    const log = await crashAt("model_attempt_abandoned");
    expect(
      log.some(
        (e) => e.type === "turn_completed" && e.data.reason === "interrupted",
      ),
    ).toBe(false);
    expect(log.at(-2)?.type).toBe("cancelled");
    expect(log.at(-1)?.data).toEqual({ reason: "cancelled" });
  });

  test("after the automatic compaction_failed: recovery carries the cancel out", async () => {
    const log = await crashAt("compaction_failed");
    expect(
      log.some(
        (e) => e.type === "turn_completed" && e.data.reason === "interrupted",
      ),
    ).toBe(false);
    expect(log.at(-2)?.type).toBe("cancelled");
    expect(log.at(-1)?.data).toEqual({ reason: "cancelled" });
  });
});
