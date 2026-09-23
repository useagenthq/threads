import { describe, expect, test } from "bun:test";
import { scriptedModel } from "../../src";
import { credential } from "../../src/agent/secret";
import type { KnownEvent } from "../../src/log";
import { resume } from "../../src/loop";
import type { Model } from "../../src/model";
import { markTestKit } from "../../src/model/guard";
import type { EventDraft, Writer } from "../../src/store";
import { ROOT, unwrap } from "../store/helpers";
import { events, harness, userInput } from "./harness";
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
