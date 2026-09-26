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

/** The decoded lines of a response body, in order, with the CRLF ambiguity handled. */
async function* lines(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<string> {
  const decoder = new TextDecoder();
  const reader = body.getReader();
  let buffered = "";
  try {
    for (;;) {
      const chunk = await reader.read();
      buffered += chunk.done
        ? decoder.decode()
        : decoder.decode(chunk.value, { stream: true });
      const { lines: ready, rest } = split(buffered, chunk.done === true);
      buffered = rest;
      for (const line of ready) yield line;
      if (chunk.done === true) return;
    }
  } finally {
    reader.releaseLock();
  }
}

/**
 * A response body as SSE events. A stream that ends mid-event drops the partial one, as the
 * event-stream rules say: an unterminated block was never dispatched.
 */
export async function* sseEvents(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<SseEvent> {
  let id: string | undefined;
  let data: string[] = [];
  for await (const line of lines(body)) {
    if (line === "") {
      // A blank line dispatches, and only a block that carried data is an event.
      if (data.length > 0) yield { id, data: data.join("\n") };
      data = [];
      continue;
    }
    const parsed = fieldOf(line);
    if (parsed.field === "data") data.push(parsed.value);
    else if (parsed.field === "id") id = parsed.value;
  }
}
