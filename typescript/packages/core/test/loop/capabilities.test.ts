import { describe, expect, test } from "bun:test";
import { sha256Hex } from "../../src/hash";
import { resume } from "../../src/loop";
import type { Model } from "../../src/model";
import { markTestKit } from "../../src/model/guard";
import type { SendError } from "../../src/model/protocol";
import type { EventDraft, Writer } from "../../src/store";
import { ROOT, unwrap, userInput } from "../store/helpers";
import { events, harness } from "./harness";

// The loop checks a rendered request against the model's declared capabilities before any
// model_request: an unsupported part ends the run with its typed code, never a provider round
// trip.

const encoder = new TextEncoder();
const usage = { input_tokens: 10, output_tokens: 2 };
const NEVER = {
  content: [{ type: "text", text: "never" }],
  stop_reason: "end_turn",
  usage,
};

const requests = (writer: Writer): number =>
  events(writer).filter((e) => e.type === "model_request").length;

describe("capability pre-check before dispatch", () => {
  test("an image to a text-only model is content_unsupported; nothing is sent", async () => {
    const h = harness([], [], [NEVER]);
    const png = encoder.encode("png");
    h.artifacts.put(png);
    const input: EventDraft = {
      type: "user_input",
      type_version: 1,
      critical: true,
      actor: {
        kind: "user",
        principal: { issuer: "api", tenant: "acme", subject: "alice" },
      },
      data: {
        source: "api",
        content: [
          { type: "text", text: "What is this?" },
          {
            type: "image_ref",
            ref: {
              sha256: sha256Hex(png),
              bytes: png.length,
              media_type: "image/png",
            },
            width: 1,
            height: 1,
          },
        ],
      },
    };
    const writer = unwrap(h.store.acquire(ROOT, "owner"));
    const end = await resume(writer, h.artifacts, h.config(), { input });
    expect(end).toEqual({ kind: "idle" });
    expect(events(writer).at(-1)).toMatchObject({
      type: "turn_completed",
      data: { reason: "error", code: "content_unsupported" },
    });
    expect(requests(writer)).toBe(0);
    expect(h.model.remaining()).toBe(1);
  });

  test("another provider's reasoning is continuation_unsupported; nothing is sent", async () => {
    const block = encoder.encode('{"type":"thinking","signature":"s"}');
    const reasoning = {
      type: "reasoning",
      provider: "anthropic",
      model: "claude-sonnet-5",
      format: "thinking",
      ref: {
        sha256: sha256Hex(block),
        bytes: block.length,
        media_type: "application/json",
      },
    };
    const h = harness(
      [],
      [],
      [
        {
          content: [reasoning, { type: "text", text: "Hi." }],
          stop_reason: "end_turn",
          usage,
        },
        NEVER,
      ],
    );
    h.artifacts.put(block);
    const writer = unwrap(h.store.acquire(ROOT, "owner"));
    const first = await resume(writer, h.artifacts, h.config(), {
      input: userInput("hi"),
    });
    expect(first).toEqual({ kind: "idle" });
    const end = await resume(writer, h.artifacts, h.config(), {
      input: userInput("again"),
    });
    expect(end).toEqual({ kind: "idle" });
    expect(events(writer).at(-1)).toMatchObject({
      type: "turn_completed",
      data: { reason: "error", code: "continuation_unsupported" },
    });
    expect(requests(writer)).toBe(1);
    expect(h.model.remaining()).toBe(1);
  });
});

/** A model whose one send ends in `reason`, counting its sends. */
function refusing(h: ReturnType<typeof harness>, reason: SendError) {
  let sends = 0;
  const model: Model = {
    info: h.model.info,
    send: async function* () {
      sends += 1;
      yield { kind: "rejected", reason };
    },
  };
  markTestKit(model);
  return { model, sends: () => sends };
}

describe("the send's terminal error (Model.send returns.errors)", () => {
  test.each([
    "continuation_unsupported",
    "transport_fence_unsupported",
  ] as const)(
    "an adapter's send-time refusal %s is not_sent and ends the turn with its code, once",
    async (code) => {
      const h = harness([], [], []);
      const m = refusing(h, code);
      const writer = unwrap(h.store.acquire(ROOT, "owner"));
      const end = await resume(
        writer,
        h.artifacts,
        h.config({ models: () => m.model }),
        { input: userInput("hi") },
      );
      expect(end).toEqual({ kind: "idle" });
      expect(m.sends()).toBe(1);
      expect(events(writer).slice(-2)).toMatchObject([
        {
          type: "model_attempt_abandoned",
          data: { provider_outcome: "not_sent", reason: "provider_error" },
        },
        {
          type: "turn_completed",
          data: { reason: "error", code },
        },
      ]);
    },
  );

  test("stale_epoch appends nothing more and halts: the writer lost its lease", async () => {
    const h = harness([], [], []);
    const m = refusing(h, "stale_epoch");
    const writer = unwrap(h.store.acquire(ROOT, "owner"));
    const end = await resume(
      writer,
      h.artifacts,
      h.config({ models: () => m.model }),
      { input: userInput("hi") },
    );
    expect(end).toMatchObject({
      kind: "halted",
      halt: { code: "branch_busy" },
    });
    expect(events(writer).at(-1)?.type).toBe("model_request");
  });
});

describe("stub mode and hosted tools", () => {
  const stub = { answer: () => undefined };

  test("a live model declaring hosted tools is refused before anything runs", async () => {
    const h = harness([], [], [NEVER]);
    const live: Model = {
      info: { ...h.model.info, hosted_tools: ["web_search"] },
      send: h.model.send,
    };
    const writer = unwrap(h.store.acquire(ROOT, "owner"));
    const before = events(writer).length;
    await expect(
      resume(writer, h.artifacts, h.config({ models: () => live, stub }), {
        input: userInput("search the web"),
      }),
    ).rejects.toMatchObject({ code: "hosted_tool_unsupported" });
    expect(events(writer).length).toBe(before);
  });

  test("a test-kit model runs in stub mode", async () => {
    const h = harness([], [], [NEVER]);
    const writer = unwrap(h.store.acquire(ROOT, "owner"));
    const end = await resume(writer, h.artifacts, h.config({ stub }), {
      input: userInput("hi"),
    });
    expect(end).toEqual({ kind: "idle" });
  });
});
