// SSE in both directions. Writing: one `data:` line per item, with an `id:` so `Last-Event-ID`
// resumes exactly. Reading: a peer's stream, frame by frame, with the field parsing the WHATWG
// event-stream rules require (a `\r\n`, `\n` or `\r` line break; a blank line dispatches; a leading
// space after the colon is dropped; several `data:` lines join with `\n`; a `:` comment is ignored).

export type SseFrame = {
  /** Absent when the item has no resumable position of its own. */
  readonly id?: string;
  readonly data: string;
};

const utf8 = new TextEncoder();

export function sseBody(
  frames: AsyncIterable<SseFrame>,
): ReadableStream<Uint8Array> {
  const iterator = frames[Symbol.asyncIterator]();
  return new ReadableStream({
    async pull(controller) {
      const next = await iterator.next();
      if (next.done === true) {
        controller.close();
        return;
      }
      const { id, data } = next.value;
      controller.enqueue(
        utf8.encode(
          `${id === undefined ? "" : `id: ${id}\n`}data: ${data}\n\n`,
        ),
      );
    },
    async cancel() {
      await iterator.return?.();
    },
  });
}

export type SseEvent = {
  readonly id: string | undefined;
  readonly data: string;
};

/** One `field: value` line, with the field names we act on kept apart from the ones we ignore. */
type Field =
  | { readonly field: "data"; readonly value: string }
  | { readonly field: "id"; readonly value: string }
  | { readonly field: "other" };

/** A line's field and value. A leading space after the colon belongs to the delimiter, not the
 * value; a line with no colon is a field with an empty value; a line starting with `:` is a
 * comment. An `id` holding a NUL is ignored, as the event-stream rules require. */
function fieldOf(line: string): Field {
  if (line.startsWith(":")) return { field: "other" };
  const colon = line.indexOf(":");
  const name = colon === -1 ? line : line.slice(0, colon);
  const raw = colon === -1 ? "" : line.slice(colon + 1);
  const value = raw.startsWith(" ") ? raw.slice(1) : raw;
  if (name === "data") return { field: "data", value };
  if (name === "id" && !value.includes("\0")) return { field: "id", value };
  return { field: "other" };
}

/**
 * The complete lines in `text`, and what is left over. A trailing `\r` may be the first half of a
 * `\r\n`, so unless the stream has ended it waits for the next chunk rather than being taken as a
 * break of its own.
 */
function split(
  text: string,
  ended: boolean,
): { readonly lines: readonly string[]; readonly rest: string } {
  const holdCr = !ended && text.endsWith("\r");
  const lines = (holdCr ? text.slice(0, -1) : text).split(/\r\n|\n|\r/);
  // The last piece has no line break yet: it stays buffered, with the held "\r" behind it.
  const rest = `${lines.pop() ?? ""}${holdCr ? "\r" : ""}`;
  return { lines, rest };
}

/**
 * A frame over this many bytes is refused rather than buffered. One `data:` block is a task or a
 * status update, so it is small; without a cap a peer can hold a line open forever and we would
 * accumulate it all and then drop it silently. The check is on the buffered string's length in
 * UTF-16 units, which is never more than its UTF-8 byte length, so the refusal can only come at or
 * after this many bytes and never before.
 */
export const MAX_FRAME_BYTES: number = 1 << 20;

/**
 * One read off a peer's stream: an event, or why the stream stopped being readable. A refusal is a
 * value and the last thing the stream yields, because a caller that cannot tell a clean end from a
 * corrupt one has no way to report what happened.
 */
export type SseRead =
  | { readonly kind: "event"; readonly event: SseEvent }
  | { readonly kind: "refused"; readonly why: string };

type LineRead =
  | { readonly kind: "line"; readonly line: string }
  | { readonly kind: "refused"; readonly why: string };

/**
 * The decoded lines of a response body, in order, with the CRLF ambiguity handled. The decoder is
 * fatal: invalid UTF-8 from a peer is a refusal, never a U+FFFD we pass on as if it were sent. A
 * replacement character is legal inside a JSON string, so a lenient decode corrupts text quietly
 * and the frame still parses, which is the one failure we must never hand a caller.
 */
async function* lines(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<LineRead> {
  const decoder = new TextDecoder(undefined, { fatal: true });
  const reader = body.getReader();
  let buffered = "";
  try {
    for (;;) {
      let text: string;
      let done: boolean;
      try {
        const chunk = await reader.read();
        done = chunk.done === true;
        text = chunk.done
          ? decoder.decode()
          : decoder.decode(chunk.value, { stream: true });
      } catch (error) {
        yield {
          kind: "refused",
          why: `the stream is not valid UTF-8: ${error instanceof Error ? error.message : String(error)}`,
        };
        return;
      }
      buffered += text;
      const { lines: ready, rest } = split(buffered, done);
      buffered = rest;
      for (const line of ready) yield { kind: "line", line };
      if (done) return;
      if (buffered.length > MAX_FRAME_BYTES) {
        yield {
          kind: "refused",
          why: `a single stream line is over ${MAX_FRAME_BYTES} bytes`,
        };
        return;
      }
    }
  } finally {
    reader.releaseLock();
  }
}

/**
 * A response body as SSE events. A stream that ends mid-event drops the partial one, as the
 * event-stream rules say: an unterminated block was never dispatched. A block whose data grows past
 * the budget, and a body that is not UTF-8, are refused instead, and the refusal ends the stream.
 */
export async function* sseEvents(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<SseRead> {
  let id: string | undefined;
  let data: string[] = [];
  let size = 0;
  for await (const read of lines(body)) {
    if (read.kind === "refused") {
      yield read;
      return;
    }
    const { line } = read;
    if (line === "") {
      // A blank line dispatches, and only a block that carried data is an event.
      if (data.length > 0)
        yield { kind: "event", event: { id, data: data.join("\n") } };
      data = [];
      size = 0;
      continue;
    }
    const parsed = fieldOf(line);
    if (parsed.field === "data") {
      size += parsed.value.length;
      if (size > MAX_FRAME_BYTES) {
        yield {
          kind: "refused",
          why: `one stream frame carries over ${MAX_FRAME_BYTES} bytes of data`,
        };
        return;
      }
      data.push(parsed.value);
    } else if (parsed.field === "id") id = parsed.value;
  }
}
