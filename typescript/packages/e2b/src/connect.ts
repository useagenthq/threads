import { z } from "zod";
import { bounded, type Send } from "./transport";
import { E2bError, excerpt, parsed } from "./wire";

// The Connect protocol with its JSON codec, as envd speaks it (connectrpc.com/docs/protocol).
// A server-streaming call sends one enveloped JSON message and reads a stream of envelopes: a
// flags byte and a big-endian uint32 length, then that many bytes of JSON, closed by an
// end-stream envelope that carries any error. A unary call is plain JSON, its error carried
// in the HTTP status. The framing is a parse boundary: every bad frame is a typed error.

/** The largest message threads reads from envd. */
export const MAX_MESSAGE_BYTES: number = 4 * 1024 * 1024;
const COMPRESSED = 0b01;
const END_STREAM = 0b10;
const HEADER = 5;

/** A Connect error: a unary call's body on a failed status, or an end-stream's `error`. */
const ConnectFailure = z.object({
  code: z.string(),
  message: z.string().optional(),
});
const EndStream = z.object({ error: ConnectFailure.optional() });

const utf8 = new TextEncoder();
const text = new TextDecoder();

const broken = (why: string) =>
  new E2bError("unavailable", `envd's stream is malformed: ${why}`);

/** One message as a request envelope (no flags). */
export function envelope(message: unknown): Uint8Array<ArrayBuffer> {
  const body = utf8.encode(JSON.stringify(message));
  const out = new Uint8Array(HEADER + body.length);
  new DataView(out.buffer).setUint32(1, body.length);
  out.set(body, HEADER);
  return out;
}

type Frame = { readonly end: boolean; readonly data: Uint8Array };
type Header = { readonly end: boolean; readonly length: number };

function header(bytes: Uint8Array): Header {
  const view = new DataView(bytes.buffer, bytes.byteOffset, HEADER);
  const flags = view.getUint8(0);
  if (flags & COMPRESSED)
    throw broken("a compressed message was never asked for");
  if (flags & ~END_STREAM) throw broken(`unknown flags ${flags}`);
  const length = view.getUint32(1);
  if (length > MAX_MESSAGE_BYTES)
    throw broken(`a ${length}-byte message is over ${MAX_MESSAGE_BYTES}`);
  return { end: flags === END_STREAM, length };
}

/** The frames of a body, each joined once from the chunks that carry it. */
async function* frames(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<Frame> {
  const parts: Uint8Array[] = [];
  let size = 0;
  const take = (n: number): Uint8Array => {
    const out = new Uint8Array(n);
    for (let at = 0; at < n; ) {
      const head = parts.shift();
      if (head === undefined) throw broken("a frame ran past its data");
      const used = Math.min(n - at, head.length);
      out.set(head.subarray(0, used), at);
      if (used < head.length) parts.unshift(head.subarray(used));
      at += used;
    }
    size -= n;
    return out;
  };
  let pending: Header | undefined;
  for await (const chunk of body) {
    parts.push(chunk);
    size += chunk.length;
    while (true) {
      if (pending === undefined && size >= HEADER)
        pending = header(take(HEADER));
      if (pending === undefined || size < pending.length) break;
      yield { end: pending.end, data: take(pending.length) };
      pending = undefined;
    }
  }
  if (pending !== undefined || size > 0) throw broken("a truncated frame");
}

/** A stream's messages as JSON; its end-stream error, or a missing end, throws. */
async function* messages(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<string> {
  for await (const frame of frames(body)) {
    const json = text.decode(frame.data);
    if (!frame.end) {
      yield json;
      continue;
    }
    const { error } = parsed(EndStream, json, "end-stream message");
    if (error !== undefined)
      throw new E2bError(
        "unavailable",
        `envd: ${error.code}: ${error.message ?? ""}`,
      );
    return;
  }
  throw broken("it ended without its end-stream message");
}

async function refused(res: Response): Promise<E2bError> {
  return new E2bError(
    "unavailable",
    `envd ${res.status}: ${await excerpt(res)}`,
  );
}

/** Calls a server-streaming procedure; its response messages as JSON text. */
export async function serverStream(
  send: Send,
  url: string,
  headers: Readonly<Record<string, string>>,
  message: unknown,
): Promise<AsyncGenerator<string>> {
  const res = await send(url, {
    method: "POST",
    headers: {
      ...headers,
      "connect-protocol-version": "1",
      "content-type": "application/connect+json",
    },
    body: envelope(message),
  });
  const type = res.headers.get("content-type") ?? "";
  if (res.status !== 200 || !type.startsWith("application/connect+json"))
    throw await refused(res);
  if (res.body === null) throw broken("no body");
  return messages(res.body);
}

export type Unary =
  | { readonly ok: true; readonly json: string }
  | { readonly ok: false; readonly code: string; readonly message: string };

/** Calls a unary procedure: its answer, or the Connect error code its status carried. */
export async function unary(
  send: Send,
  url: string,
  headers: Readonly<Record<string, string>>,
  message: unknown,
): Promise<Unary> {
  const res = await send(
    url,
    bounded({
      method: "POST",
      headers: {
        ...headers,
        "connect-protocol-version": "1",
        "content-type": "application/json",
      },
      body: JSON.stringify(message),
    }),
  );
  const body = await res.text();
  if (res.status === 200) return { ok: true, json: body };
  if (!(res.headers.get("content-type") ?? "").startsWith("application/json"))
    throw new E2bError(
      "unavailable",
      `envd ${res.status}: ${body.slice(0, 500)}`,
    );
  const failure = parsed(ConnectFailure, body, "Connect error");
  return { ok: false, code: failure.code, message: failure.message ?? "" };
}
