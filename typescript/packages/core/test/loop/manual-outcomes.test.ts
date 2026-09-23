import { describe, expect, test } from "bun:test";
import { scriptedModel } from "../../src";
import { credential } from "../../src/agent/secret";
import type { KnownEvent } from "../../src/log";
import { resume } from "../../src/loop";
import { CONTEXT_DEFAULTS } from "../../src/loop/policy";
import type { Model } from "../../src/model";
import { markTestKit } from "../../src/model/guard";
import type { EventDraft } from "../../src/store";
import { ROOT, unwrap } from "../store/helpers";
import { events } from "./harness";
import {
  asked,
  fail,
  outcome,
  reply,
  resumeOnce,
  SUMMARY_TEXT,
  sides,
} from "./manual-kit";

// Every way a requested compaction's side attempt can end is answered by exactly one outcome
// naming the request (spec/schema/README.md, "Requested compaction and output styles").

const outcomes = (log: readonly KnownEvent[]): readonly KnownEvent[] =>
  log.filter((e) => e.type === "compacted" || e.type === "compaction_failed");

function answered(log: readonly KnownEvent[], data: Record<string, unknown>) {
  expect(outcomes(log)).toHaveLength(1);
  expect(outcome(log)).toMatchObject({ data });
}

describe("requested compaction outcomes", () => {
  test("a summary: compacted{manual} naming the request, then the turn answers", async () => {
    const h = asked([reply(SUMMARY_TEXT), reply("Answer.")]);
    const log = await resumeOnce(h);
    const [request] = sides(log);
    if (request?.type !== "model_request") throw new Error("a side request");
    const cause = request.data.cause_event_id;
    expect(cause).toBeString();
    answered(log, { trigger: "manual", cause_event_id: cause });
    expect(log.at(-1)).toMatchObject({ type: "turn_completed" });
  });

  test("an empty summary is empty_summary", async () => {
    const h = asked([reply(""), reply("Answer.")]);
    answered(await resumeOnce(h), {
      stage: "summary",
      reason: "empty_summary",
    });
  });

  test("prompt_too_long twice: one fallback attempt, then prompt_too_long", async () => {
    const h = asked([
      fail("prompt_too_long", 400),
      fail("prompt_too_long", 400),
      reply("A."),
    ]);
    const log = await resumeOnce(h);
    expect(sides(log)).toHaveLength(2);
    answered(log, { reason: "prompt_too_long" });
  });

  test("a provider error is model_error, with no second side request", async () => {
    const h = asked([fail("server_error", 500), reply("A.")]);
    const log = await resumeOnce(h);
    expect(sides(log)).toHaveLength(1);
    answered(log, { reason: "model_error" });
  });

  test("no adapter for the epoch's model is model_error", async () => {
    const h = asked([]);
    const log = await resumeOnce(h, { models: () => undefined });
    expect(sides(log)).toHaveLength(0);
    answered(log, { reason: "model_error" });
  });

  test("a render failure (a missing artifact) is artifact_error", async () => {
    const missing = {
      type: "injected",
      type_version: 1,
      critical: true,
      actor: { kind: "host" },
      data: {
        source: "attachment",
        trust: "untrusted_reference",
        origin: { id: "gone.txt" },
        ref: { sha256: "f".repeat(64), bytes: 3, media_type: "text/plain" },
      },
    } as const;
    const h = asked([], undefined, [missing]);
    const log = await resumeOnce(h);
    answered(log, { reason: "artifact_error" });
  });

  test("a before_compact deny is hook_denied, with no side request", async () => {
    const h = asked([reply("Answer.")]);
    const deny = {
      name: "guard",
      timeoutMs: 1000,
      hooks: {
        before_compact: async () =>
          ({ decision: "deny", reason: "no" }) as const,
      },
    };
    const log = await resumeOnce(h, { extensions: [deny] });
    expect(sides(log)).toHaveLength(0);
    answered(log, { stage: "hook", reason: "hook_denied" });
  });

  test("the breaker being open doesn't stop a request", async () => {
    const failed = {
      type: "compaction_failed",
      type_version: 1,
      critical: true,
      actor: { kind: "host" },
      data: { stage: "summary", reason: "still_over_threshold" },
    } as const;
    const policy = { context: CONTEXT_DEFAULTS };
    const h = asked([reply(SUMMARY_TEXT), reply("A.")], policy, [
      failed,
      failed,
      failed,
    ]);
    answered(await resumeOnce(h), { trigger: "manual" });
  });
});

/** The harness model with no declared window, running `before` inside each send. */
function around(h: ReturnType<typeof asked>, before: () => void): Model {
  const inner = h.model;
  const model: Model = {
    ...inner,
    info: {
      ...inner.info,
      limits: { ...inner.info.limits, context_window: 0 },
    },
    send: (request, ctx, options) => {
      before();
      return inner.send(request, ctx, options);
    },
  };
  markTestKit(model);
  return model;
}

const operator = { issuer: "api", tenant: "acme", subject: "operator" };

describe("requested compaction around the side attempt", () => {
  test("a cancel during the attempt: its outcome is recorded, then the turn is cancelled", async () => {
    const h = asked([reply(SUMMARY_TEXT)]);
    h.clock.now += 60_000;
    const writer = unwrap(h.store.acquire(ROOT, "owner"));
    const cancel: EventDraft = {
      type: "cancel_requested",
      type_version: 1,
      critical: true,
      actor: { kind: "user", principal: operator },
      data: { scope: "thread" },
    };
    // The model declares no window: a request is carried out all the same.
    const model = around(h, () => unwrap(writer.append([cancel])));
    await resume(writer, h.artifacts, h.config({ models: () => model }));
    const log = events(writer);
    const types = log.map((e) => e.type);
    expect(types.slice(types.indexOf("cancel_requested"))).toEqual([
      "cancel_requested",
      "model_response",
      "compacted",
      "cancelled",
      "turn_completed",
    ]);
    expect(log.filter((e) => e.type === "model_request")).toHaveLength(1);
  });

  test("a lease lost during the attempt: no outcome; the new owner finishes it", async () => {
    const h = asked([reply(SUMMARY_TEXT), reply(SUMMARY_TEXT), reply("A.")]);
    h.clock.now += 60_000;
    const writer = unwrap(h.store.acquire(ROOT, "owner"));
    const steal = around(h, () => {
      h.clock.now += writer.lease.ttlMs + 1;
      unwrap(h.store.acquire(ROOT, "thief")).release();
    });
    const end = await resume(
      writer,
      h.artifacts,
      h.config({ models: () => steal }),
    );
    expect(end).toMatchObject({
      kind: "halted",
      halt: { code: "branch_busy" },
    });
    expect(outcome(events(writer))).toBeUndefined();
    const log = await resumeOnce(h);
    expect(outcome(log)).toMatchObject({ type: "compacted" });
  });
});

describe("a requested summary refused as leaked", () => {
  test("records exactly one compaction_failed, and the turn ends secret_in_provider_output", async () => {
    const key = credential("fake", "apiKey", "sk-l17-leak-5e6f", "U")();
    const encoder = new TextEncoder();
    const leaky: Model = {
      info: scriptedModel({ responses: [] }).info,
      send: async function* (_request, context) {
        // Provider material that can't be redacted, holding a registered secret.
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
    markTestKit(leaky);
    const h = asked([]);
    const log = await resumeOnce(h, { models: () => leaky });
    expect(sides(log)).toHaveLength(1);
    const failed = log.filter((e) => e.type === "compaction_failed");
    expect(failed).toHaveLength(1);
    const request = log.find((e) => e.type === "model_request");
    expect(failed[0]).toMatchObject({
      data: {
        stage: "summary",
        reason: "model_error",
        request_event_id: request?.event_id,
      },
    });
    expect(log.slice(-3).map((e) => e.type)).toEqual([
      "model_attempt_abandoned",
      "compaction_failed",
      "turn_completed",
    ]);
    expect(log.at(-1)).toMatchObject({
      data: { reason: "error", code: "secret_in_provider_output" },
    });
  });
});
