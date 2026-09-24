/** Exact reads over a chunked byte source; `offset` counts the bytes taken. */
export class Source {
  offset = 0;
  #head: Uint8Array = new Uint8Array();
  readonly #it: AsyncIterator<Uint8Array>;

  constructor(source: AsyncIterable<Uint8Array>) {
    this.#it = source[Symbol.asyncIterator]();
  }

  /** The next bytes, at most `max`; undefined at the end of the source. */
  async next(max: number): Promise<Uint8Array | undefined> {
    while (this.#head.length === 0) {
      const next = await this.#it.next();
      if (next.done === true) return undefined;
      this.#head = next.value;
    }
    const piece = this.#head.subarray(0, max);
    this.#head = this.#head.subarray(piece.length);
    this.offset += piece.length;
    return piece;
  }

  /** Passes the next `n` bytes to `each`; false when the source ends first. */
  async take(n: number, each: (piece: Uint8Array) => void): Promise<boolean> {
    for (let left = n; left > 0; ) {
      const piece = await this.next(left);
      if (piece === undefined) return false;
      each(piece);
      left -= piece.length;
    }
    return true;
  }

  /** The next `n` bytes; undefined when the source ends first. */
  async exact(n: number): Promise<Uint8Array | undefined> {
    const out = new Uint8Array(n);
    let at = 0;
    const whole = await this.take(n, (piece) => {
      out.set(piece, at);
      at += piece.length;
    });
    return whole ? out : undefined;
  }

  /** Stops the source early, so its producer can release what it holds. */
  async close(): Promise<void> {
    await this.#it.return?.();
  }
}
