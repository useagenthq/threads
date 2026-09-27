import { describe, expect, test } from "bun:test";
import { MAX_FRAME_BYTES, type SseRead, sseEvents } from "../src/protocol/sse";

// Reading a peer's SSE stream. The WHATWG field rules are pinned by the shared vector
// (vectors.test.ts); what is pinned here is what the reader does with bytes it cannot read, because
// a stream that ends quietly is indistinguishable from one that finished.

function body(chunks: readonly Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      controller.close();
    },
  });
}

const bytes = (...parts: readonly (string | number)[]): Uint8Array =>
  Uint8Array.from(
    parts.flatMap((p) =>
      typeof p === "number" ? [p] : [...new TextEncoder().encode(p)],
    ),
  );

async function readAll(
  chunks: readonly Uint8Array[],
): Promise<readonly SseRead[]> {
  return await Array.fromAsync(sseEvents(body(chunks)));
}

describe("a character split across two chunks", () => {
  test("is one character, not two replacement characters", async () => {
    // "é" is 0xC3 0xA9, and a real socket splits wherever it likes. Decoding each chunk alone gave
    // two U+FFFD, and U+FFFD is legal inside a JSON string, so the frame still parsed and the
    // corrupted text was handed on as if the peer had sent it.
    const reads = await readAll([
      bytes('data: {"t":"', 0xc3),
      bytes(0xa9, '"}\n\n'),
    ]);
    expect(reads).toEqual([
      { kind: "event", event: { id: undefined, data: '{"t":"é"}' } },
    ]);
  });

  test("survives being split one byte at a time", async () => {
    const whole = bytes('data: {"t":"é"}\n\n');
    const reads = await readAll([...whole].map((b) => Uint8Array.of(b)));
    expect(reads).toEqual([
      { kind: "event", event: { id: undefined, data: '{"t":"é"}' } },
    ]);
  });
});

describe("bytes that are not UTF-8", () => {
  test("refuse, rather than becoming a replacement character we pass on", async () => {
    const reads = await readAll([bytes('data: {"t":"', 0xff, '"}\n\n')]);
    expect(reads.map((r) => r.kind)).toEqual(["refused"]);
    if (reads[0]?.kind !== "refused") throw new Error("unreachable");
    expect(reads[0].why).toContain("UTF-8");
  });

  test("a stream that ends mid-character refuses rather than truncating it", async () => {
    const reads = await readAll([bytes('data: {"t":"', 0xc3)]);
    expect(reads.map((r) => r.kind)).toEqual(["refused"]);
  });

  test("events before the bad bytes are still delivered, and nothing after", async () => {
    const reads = await readAll([
      bytes("data: first\n\n"),
      bytes("data: ", 0xff, "\n\n"),
      bytes("data: third\n\n"),
    ]);
    expect(reads.map((r) => r.kind)).toEqual(["event", "refused"]);
  });
});

describe("the frame budget", () => {
  test("an unterminated line past the budget refuses rather than being buffered and dropped", async () => {
    const chunk = bytes("a".repeat(256 * 1024));
    // 2 MiB of one line with no break: it used to be accumulated whole and then dropped in silence.
    const reads = await readAll(Array.from({ length: 8 }, () => chunk));
    expect(reads.map((r) => r.kind)).toEqual(["refused"]);
    if (reads[0]?.kind !== "refused") throw new Error("unreachable");
    expect(reads[0].why).toContain(String(MAX_FRAME_BYTES));
  });

  test("a frame whose data lines add up past the budget refuses", async () => {
    const line = `data: ${"a".repeat(64 * 1024)}\n`;
    const reads = await readAll([bytes(line.repeat(24))]);
    expect(reads.map((r) => r.kind)).toEqual(["refused"]);
  });

  test("a stream of ordinary frames is not touched by the budget", async () => {
    const reads = await readAll([bytes("data: a\n\ndata: b\n\ndata: c\n\n")]);
    expect(reads.map((r) => r.kind)).toEqual(["event", "event", "event"]);
  });
});
