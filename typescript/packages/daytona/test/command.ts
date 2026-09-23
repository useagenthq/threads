import type { Sinks, Started } from "@threads/core/adapter";
import type { LogSocket } from "../src/logs";

// One mocked session command: its output in Daytona's marked form (01 01 01 before stdout,
// 02 02 02 before stderr), its exit code, and the log WebSocket that replays both.

type Listener = (event: { readonly data?: unknown }) => void;

export class Command {
  readonly chunks: Uint8Array[] = [];
  exit: number | undefined;
  private readonly wakes = new Set<() => void>();

  /** Runs `start` with sinks that record marked output, then records its exit code. */
  constructor(start: (sinks: Sinks) => Started) {
    const started = start({
      stdout: (b) => this.push(1, b),
      stderr: (b) => this.push(2, b),
    });
    const finish = async () => {
      this.exit = await started.exit;
      this.wake();
    };
    void finish();
  }

  private push(mark: number, bytes: Uint8Array): void {
    this.chunks.push(new Uint8Array([mark, mark, mark, ...bytes]));
    this.wake();
  }

  private wake(): void {
    for (const wake of this.wakes) wake();
  }

  /** A socket replaying the output, each frame split in two, closing after the exit. */
  socket(): LogSocket {
    const messages: Listener[] = [];
    const closes: Listener[] = [];
    let sent = 0;
    let closed = false;
    const send = (chunk: Uint8Array) => {
      const half = Math.ceil(chunk.length / 2);
      for (const part of [chunk.slice(0, half), chunk.slice(half)])
        for (const listener of messages) listener({ data: part.buffer });
    };
    const flush = () => {
      for (; sent < this.chunks.length; sent += 1)
        send(this.chunks[sent] ?? new Uint8Array());
      if (closed || this.exit === undefined) return;
      closed = true;
      for (const listener of closes) listener({});
    };
    queueMicrotask(() => {
      this.wakes.add(flush);
      flush();
    });
    return {
      binaryType: "blob",
      addEventListener: (type, listener) => {
        if (type === "message") messages.push(listener);
        if (type === "close") closes.push(listener);
      },
    };
  }
}
