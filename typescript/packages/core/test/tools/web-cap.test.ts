import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { WEB_FETCH_MAX_BYTES } from "../../src/tools/generated/limits";
import { webFetch } from "../../src/tools/web-fetch";
import type { WebTransport } from "../../src/tools/web-transport";
import { bound } from "./kit";

// Where web_fetch cuts a response, against the shared vector
// (spec/conformance/vectors/web-fetch-cap.json). The cap is one number for both languages
// (spec/schema/limits.json, generated into each), because it is the truncation boundary: cut at a
// different byte and the same URL leaves a different content hash in the log. The Python twin is
// tests/web/test_fetch_cap.py, over the same vector.

const Vector = z.strictObject({
  description: z.string(),
  max_bytes: z.int(),
  cases: z.array(
    z.strictObject({
      name: z.string(),
      served: z.int(),
      kept: z.int(),
      sha256: z.string(),
    }),
  ),
});

const VECTOR = Vector.parse(
  JSON.parse(
    readFileSync(
      join(
        import.meta.dir,
        "../../../../../spec/conformance/vectors/web-fetch-cap.json",
      ),
      "utf8",
    ),
  ),
);

/** Byte i is i % 251, as the vector's generator builds it. */
function pattern(n: number): Uint8Array {
  const out = new Uint8Array(n);
  for (let i = 0; i < n; i += 1) out[i] = i % 251;
  return out;
}

const CHUNK = 64 * 1024;

/** The body in chunks, so the reader's accumulate-and-cut runs as it does over a socket. */
function chunked(bytes: Uint8Array): ReadableStream<Uint8Array> {
  let at = 0;
  return new ReadableStream<Uint8Array>({
    pull: (controller) => {
      if (at >= bytes.length) {
        controller.close();
        return;
      }
      controller.enqueue(
        bytes.subarray(at, Math.min(at + CHUNK, bytes.length)),
      );
      at += CHUNK;
    },
  });
}

function serving(served: number): WebTransport {
  return {
    resolve: async () => ["93.184.216.34"],
    fetch: async () =>
      new Response(chunked(pattern(served)), {
        status: 200,
        headers: { "content-type": "text/plain" },
      }),
  };
}

describe("web_fetch's byte cap", () => {
  test("the cap the code uses is the vector's", () => {
    expect(WEB_FETCH_MAX_BYTES).toBe(VECTOR.max_bytes);
  });

  test.each(VECTOR.cases.map((c) => [c.name, c] as const))(
    "%s",
    async (_name, expected) => {
      const tool = bound(webFetch(serving(expected.served)));
      const run = await tool.run({ url: "https://example.com/p" });
      if (run.kind !== "done") throw new Error(run.kind);
      // The hash the result records is the hash of the kept bytes, and the cited artifact is them.
      expect(run.output).toContain(`SHA-256: ${expected.sha256}`);
      const cite = run.content?.[1];
      if (cite?.type !== "citation" || cite.ref === undefined)
        throw new Error("no citation");
      expect(cite.ref.sha256).toBe(expected.sha256);
      expect(cite.ref.bytes).toBe(expected.kept);
    },
  );
});
