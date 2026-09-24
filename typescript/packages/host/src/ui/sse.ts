import { canonicalize } from "@threads/core/host";
import type { Protocol } from "./frame";
import type { Out } from "./stream";

// A UI stream on the wire: one SSE message per frame, its data the frame's chunk as canonical
// JSON (RFC 8785) and its id the frame's <seq>:<k> when it has one. An AI SDK stream that ends
// cleanly sends data: [DONE]; a broken one just closes, so the client falls back to a replay.

const HEADERS: Readonly<Record<Protocol, Readonly<Record<string, string>>>> = {
  "ai-sdk": {
    "content-type": "text/event-stream",
    "cache-control": "no-cache",
    "x-vercel-ai-ui-message-stream": "v1",
    "x-accel-buffering": "no",
  },
  "ag-ui": {
    "content-type": "text/event-stream",
    "cache-control": "no-cache",
    "x-accel-buffering": "no",
  },
};

export function sseResponse(
  protocol: Protocol,
  frames: AsyncGenerator<Out, void, undefined>,
): Response {
  const utf8 = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    async pull(controller) {
      const next = await frames.next();
      if (next.done === true) {
        controller.close();
        return;
      }
      const out = next.value;
      if ("end" in out) {
        if (out.end === "done" && protocol === "ai-sdk")
          controller.enqueue(utf8.encode("data: [DONE]\n\n"));
        controller.close();
        await frames.return();
        return;
      }
      const data = canonicalize(out.data);
      if (!data.ok) throw new Error(`a frame is JSON: ${data.error.message}`);
      const id = out.id === undefined ? "" : `id: ${out.id}\n`;
      controller.enqueue(utf8.encode(`${id}data: ${data.value}\n\n`));
    },
    async cancel() {
      await frames.return();
    },
  });
  return new Response(body, { status: 200, headers: HEADERS[protocol] });
}
