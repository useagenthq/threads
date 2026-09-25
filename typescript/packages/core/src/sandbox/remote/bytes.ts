// A byte stream fed by a provider's output callbacks: chunks are yielded as they arrive and
// never joined, so the consumer (the sandbox exec layer) sees the output incrementally.

export type ByteStream = {
  readonly push: (chunk: Uint8Array) => void;
  readonly end: () => void;
  readonly chunks: AsyncIterable<Uint8Array>;
  /** Bytes queued and not yet read. */
  readonly buffered: () => number;
};

export function byteStream(): ByteStream {
  const queue: Uint8Array[] = [];
  let ended = false;
  let wake = Promise.withResolvers<void>();
  async function* chunks(): AsyncIterable<Uint8Array> {
    try {
      while (true) {
        const next = queue.shift();
        if (next !== undefined) yield next;
        else if (ended) return;
        else {
          await wake.promise;
          wake = Promise.withResolvers<void>();
        }
      }
    } finally {
      // A reader that stopped early (a refused archive) wants nothing more: what the
      // sandbox still sends is dropped, never queued without bound.
      ended = true;
      queue.length = 0;
    }
  }
  return {
    push: (chunk) => {
      if (chunk.length === 0 || ended) return;
      queue.push(chunk);
      wake.resolve();
    },
    end: () => {
      ended = true;
      wake.resolve();
    },
    chunks: chunks(),
    buffered: () => queue.reduce((n, c) => n + c.length, 0),
  };
}

/** Every chunk of a stream, joined: for the kit's own short script outputs only. */
export async function joined(
  stream: AsyncIterable<Uint8Array>,
): Promise<Uint8Array> {
  const parts: Uint8Array[] = [];
  for await (const chunk of stream) parts.push(chunk);
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.length;
  }
  return out;
}
