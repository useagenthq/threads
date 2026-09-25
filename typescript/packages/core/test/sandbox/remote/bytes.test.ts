import { expect, test } from "bun:test";
import { byteStream } from "../../../src/sandbox/remote/bytes";
import { readTar } from "../../../src/sandbox/tree/tar";
import { hashOnly } from "../../../src/sandbox/trees";

// A byte stream whose reader stopped early (readTree on a refused archive) drops what the
// sandbox still sends, rather than queueing it without bound.

test("pushes after the reader stops are dropped", async () => {
  const stream = byteStream();
  stream.push(new Uint8Array(512).fill(7));
  const read = await readTar(stream.chunks, hashOnly);
  expect(read.ok ? undefined : read.error.reason).toBe("bad_header");
  // A sandbox that keeps writing after the refusal: nothing is kept.
  for (let i = 0; i < 1000; i += 1) stream.push(new Uint8Array(4096));
  expect(stream.buffered()).toBe(0);
});
