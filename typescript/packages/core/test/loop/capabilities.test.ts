import { describe, expect, test } from "bun:test";
import { sha256Hex } from "../../src/hash";
import { resume } from "../../src/loop";
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
    expect(end).toMatchObject({
      kind: "halted",
      halt: { code: "content_unsupported" },
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
    expect(end).toMatchObject({
      kind: "halted",
      halt: { code: "continuation_unsupported" },
    });
    expect(requests(writer)).toBe(1);
    expect(h.model.remaining()).toBe(1);
  });
});
