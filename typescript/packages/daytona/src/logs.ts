import { fenceHere, type Sinks } from "@threads/core/adapter";

// A session command's output, followed over the toolbox's WebSocket as the Daytona SDK reads it:
// one byte stream where the markers 01 01 01 and 02 02 02 switch between stdout and stderr.
// Bytes pass through undecoded.

/** The WebSocket surface the logs need; the default is the runtime's WebSocket. */
export type LogSocket = {
  binaryType: string;
  addEventListener: (
    type: "message" | "close" | "error",
    listener: (event: { readonly data?: unknown }) => void,
  ) => void;
};

export type OpenSocket = (
  url: string,
  headers: Readonly<Record<string, string>>,
) => LogSocket;

const MARK = { 1: "stdout", 2: "stderr" } as const;

/** Splits the marked stream into sinks; a marker split across frames is held until complete. */
export function demux(sinks: Sinks): {
  readonly push: (chunk: Uint8Array) => void;
  readonly end: () => void;
} {
  let pending = new Uint8Array(0);
  let target: "stdout" | "stderr" | undefined;
  const emit = (bytes: Uint8Array) => {
    if (bytes.length > 0 && target !== undefined) sinks[target](bytes.slice());
  };
  const marker = (buf: Uint8Array, at: number) => {
    const b = buf[at];
    return (b === 1 || b === 2) && buf[at + 1] === b && buf[at + 2] === b
      ? b
      : undefined;
  };
  return {
    push: (chunk) => {
      const buf = new Uint8Array(pending.length + chunk.length);
      buf.set(pending);
      buf.set(chunk, pending.length);
      let start = 0;
      for (let at = 0; at + 2 < buf.length; ) {
        const mark = marker(buf, at);
        if (mark === undefined) at += 1;
        else {
          emit(buf.subarray(start, at));
          target = MARK[mark];
          at += 3;
          start = at;
        }
      }
      let keep = buf.length;
      while (keep > start && buf.length - keep < 2) {
        const b = buf[keep - 1];
        if (b !== 1 && b !== 2) break;
        keep -= 1;
      }
      emit(buf.subarray(start, keep));
      pending = buf.slice(keep);
    },
    end: () => {
      emit(pending);
      pending = new Uint8Array(0);
    },
  };
}

function bytesOf(data: unknown): Uint8Array {
  if (typeof data === "string") return new TextEncoder().encode(data);
  if (data instanceof ArrayBuffer) return new Uint8Array(data);
  if (data instanceof Uint8Array) return data;
  throw new Error("an unexpected WebSocket frame");
}

/** Follows the logs until the socket closes; the fence is checked as it opens. */
export async function follow(
  open: OpenSocket,
  url: string,
  headers: Readonly<Record<string, string>>,
  sinks: Sinks,
): Promise<void> {
  await fenceHere();
  const done = Promise.withResolvers<void>();
  const split = demux(sinks);
  const socket = open(url, headers);
  socket.binaryType = "arraybuffer";
  socket.addEventListener("message", (event) => {
    try {
      split.push(bytesOf(event.data));
    } catch (error) {
      done.reject(error);
    }
  });
  socket.addEventListener("close", () => {
    split.end();
    done.resolve();
  });
  socket.addEventListener("error", () =>
    done.reject(new Error(`the log stream ${url} failed`)),
  );
  return done.promise;
}
