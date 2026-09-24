// Test helpers: a seeded random source and chunked byte streams.

/** mulberry32: a small seeded generator, so a failing chunking can be replayed. */
export function seeded(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** `bytes` as chunks of random sizes from 1 to `max` (some empty), awaited one by one. */
export async function* chunked(
  bytes: Uint8Array,
  random: () => number,
  max: number,
): AsyncGenerator<Uint8Array> {
  for (let at = 0; at < bytes.length; ) {
    if (random() < 0.05) yield new Uint8Array();
    const size = 1 + Math.floor(random() * max);
    yield bytes.slice(at, at + size);
    at += size;
  }
}

/** The chunkings every vector is read with: whole, one byte at a time, and random sizes. */
export function chunkings(
  bytes: Uint8Array,
  seed: number,
): readonly (() => AsyncIterable<Uint8Array>)[] {
  const whole = async function* (): AsyncGenerator<Uint8Array> {
    yield bytes;
  };
  return [
    whole,
    () => chunked(bytes, () => 0, 1),
    ...[7, 512, 700, 5000].map(
      (max, i) => () => chunked(bytes, seeded(seed * 31 + i), max),
    ),
  ];
}
