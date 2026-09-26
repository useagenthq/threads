import { liveTransport, vet, type WebTransport } from "@threads/core/adapter";
import { type A2aFault, errorByCode, fault } from "./errors";
import { type Method, rpcOutcome } from "./jsonrpc";
import { sseEvents } from "./sse";
import { StreamResponse as StreamItem, type StreamResponse } from "./task";
import {
  A2A_JSON,
  A2A_VERSION,
  EXTENSIONS_HEADER,
  VERSION_HEADER,
} from "./version";
import { outbound, parseJson, streams, type Wire } from "./wire";

// One A2A request. The adapter owns the bytes, so the caller sees exactly which of the four
// outcomes it got, and those four are the ones the effect machinery needs:
//
//   ok        the peer answered; the result is parsed a layer up
//   fault     the peer answered an A2A error, which is an answer, not a doubt
//   not_sent  the connection was refused or the TLS handshake failed: no byte was written
//   unknown   anything else after dispatch: a timeout, or a connection that opened and broke
//
// `not_sent` is deliberately narrow. Treating a doubtful case as "not sent" would let an effect
// repeat silently (invariant 3), so only errors that happen strictly before the request is written
// count, and everything else is uncertainty.

export type Answer =
  | { readonly kind: "ok"; readonly value: unknown }
  | { readonly kind: "stream"; readonly items: AsyncGenerator<StreamResponse> }
  | { readonly kind: "fault"; readonly fault: A2aFault }
  | { readonly kind: "not_sent"; readonly why: string }
  | {
      readonly kind: "unknown";
      readonly reason: "timeout" | "transport_error";
      readonly why: string;
    };

export type Sending = {
  readonly transport?: WebTransport;
  /** Resolved at send time from a secret, sent only in the header, never stored. */
  readonly authorization?: string;
  /** The extensions this request relies on, sent as `A2A-Extensions`. */
  readonly extensions?: readonly string[];
  readonly signal: AbortSignal;
  readonly timeoutMs: number;
};

/** A response body over this many bytes is refused: a card or a task is small. */
const MAX_BYTES = 1 << 20;

export async function call(
  wire: Wire,
  method: Method,
  params: Readonly<Record<string, unknown>>,
  sending: Sending,
): Promise<Answer> {
  const built = outbound(wire, method, params);
  const transport = sending.transport ?? liveTransport;
  let url: URL;
  try {
    url = new URL(built.url);
  } catch {
    return { kind: "not_sent", why: `${built.url} is not a URL` };
  }
  if (url.protocol !== "https:")
    return {
      kind: "not_sent",
      why: `a remote is called over https, not ${url.protocol}`,
    };
  const target = await vet(url, transport);
  if ("denied" in target) return { kind: "not_sent", why: target.denied };
  const signal = AbortSignal.any([
    sending.signal,
    AbortSignal.timeout(sending.timeoutMs),
  ]);
  let response: Response;
  try {
    response = await transport.fetch(url.href, target.address, {
      method: built.verb,
      headers: headers(built.accept, built.body !== undefined, sending),
      ...(built.body === undefined ? {} : { body: built.body }),
      signal,
    });
  } catch (error) {
    return failed(error, sending.signal.aborted);
  }
  return streams(method) && response.status === 200 && isEventStream(response)
    ? { kind: "stream", items: stream(response) }
    : await single(response);
}

export type Fetched =
  | { readonly kind: "bytes"; readonly bytes: Uint8Array }
  | { readonly kind: "failed"; readonly why: string };

/**
 * A GET of `url`, up to `maxBytes`, with no credential: this is how a partner's agent card is read,
 * and discovery is unauthenticated by the spec. It is a read, so it is not an effect and it retries
 * freely; failures are one value, because "why the card could not be read" is all a caller needs.
 */
export async function fetchBytes(
  url: string,
  maxBytes: number,
  sending: Sending,
): Promise<Fetched> {
  const transport = sending.transport ?? liveTransport;
  let target: URL;
  try {
    target = new URL(url);
  } catch {
    return { kind: "failed", why: `${url} is not a URL` };
  }
  if (target.protocol !== "https:")
    return {
      kind: "failed",
      why: `a card is fetched over https, not ${target.protocol}`,
    };
  const vetted = await vet(target, transport);
  if ("denied" in vetted) return { kind: "failed", why: vetted.denied };
  let response: Response;
  try {
    response = await transport.fetch(target.href, vetted.address, {
      method: "GET",
      headers: { accept: `${A2A_JSON}, application/json` },
      signal: AbortSignal.any([
        sending.signal,
        AbortSignal.timeout(sending.timeoutMs),
      ]),
    });
  } catch (error) {
    return { kind: "failed", why: message(error) };
  }
  if (response.status !== 200)
    return { kind: "failed", why: `HTTP ${response.status}` };
  const bytes = await bodyBytes(response, maxBytes);
  return typeof bytes === "string"
    ? { kind: "failed", why: bytes }
    : { kind: "bytes", bytes };
}

function headers(
  accept: string,
  hasBody: boolean,
  sending: Sending,
): Record<string, string> {
  const extensions = sending.extensions ?? [];
  return {
    accept,
    [VERSION_HEADER]: A2A_VERSION,
    ...(hasBody ? { "content-type": A2A_JSON } : {}),
    ...(extensions.length === 0
      ? {}
      : { [EXTENSIONS_HEADER]: extensions.join(",") }),
    ...(sending.authorization === undefined
      ? {}
      : { authorization: sending.authorization }),
  };
}

function isEventStream(response: Response): boolean {
  return (response.headers.get("content-type") ?? "").includes(
    "text/event-stream",
  );
}

/**
 * The error codes that prove the request never left: the socket was refused or unreachable, the
 * name did not resolve, or the TLS handshake failed. Everything else, including a reset or a
 * broken pipe, may have written bytes first, so it is uncertainty.
 */
const NEVER_LEFT = new Set([
  "ECONNREFUSED",
  "ENOTFOUND",
  "EAI_AGAIN",
  "EHOSTUNREACH",
  "ENETUNREACH",
  "EADDRNOTAVAIL",
  "ERR_TLS_CERT_ALTNAME_INVALID",
  "CERT_HAS_EXPIRED",
  "DEPTH_ZERO_SELF_SIGNED_CERT",
  "SELF_SIGNED_CERT_IN_CHAIN",
  "UNABLE_TO_VERIFY_LEAF_SIGNATURE",
  "ERR_SSL_WRONG_VERSION_NUMBER",
]);

function failed(error: unknown, cancelled: boolean): Answer {
  const code = errorCode(error);
  const why = `${code ?? "error"}: ${message(error)}`;
  if (code !== undefined && NEVER_LEFT.has(code))
    return { kind: "not_sent", why };
  // An abort we asked for is still uncertainty: the request may already have been written.
  return {
    kind: "unknown",
    reason: cancelled || isTimeout(error) ? "timeout" : "transport_error",
    why,
  };
}

function errorCode(error: unknown): string | undefined {
  if (typeof error !== "object" || error === null) return undefined;
  const code = "code" in error ? error.code : undefined;
  return typeof code === "string" ? code : undefined;
}

function isTimeout(error: unknown): boolean {
  return (
    error instanceof Error &&
    (error.name === "TimeoutError" || error.name === "AbortError")
  );
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** A non-streaming answer: the operation's own message, or the A2A error the peer named. */
async function single(response: Response): Promise<Answer> {
  const read = await text(response);
  if (typeof read !== "string") return read;
  const json = parseJson(read);
  if (!json.ok) return { kind: "fault", fault: json.fault };
  // Both bindings can answer either way, so a JSON-RPC envelope is unwrapped wherever it appears.
  if (isEnvelope(json.value)) {
    const outcome = rpcOutcome(json.value);
    return "fault" in outcome
      ? { kind: "fault", fault: outcome.fault }
      : { kind: "ok", value: outcome.result };
  }
  if (response.status >= 400)
    return { kind: "fault", fault: httpFault(response.status, json.value) };
  return { kind: "ok", value: json.value };
}

/**
 * An HTTP+JSON error body. The binding carries the A2A error in the status, and implementations
 * put the code either at the top level or under `error`, so both are read; a body that names no
 * code we know is the status, said plainly.
 */
function httpFault(status: number, json: unknown): A2aFault {
  const named = errorByCode(codeIn(json) ?? Number.NaN);
  return named === undefined
    ? fault(
        "InvalidAgentResponseError",
        `the peer answered HTTP ${status} with no A2A error code`,
      )
    : fault(named, messageIn(json) ?? `the peer answered HTTP ${status}`);
}

function at(json: unknown, key: string): unknown {
  return typeof json === "object" && json !== null && key in json
    ? Reflect.get(json, key)
    : undefined;
}

function codeIn(json: unknown): number | undefined {
  const own = at(json, "code") ?? at(at(json, "error"), "code");
  return typeof own === "number" ? own : undefined;
}

function messageIn(json: unknown): string | undefined {
  const own = at(json, "message") ?? at(at(json, "error"), "message");
  return typeof own === "string" ? own : undefined;
}

function isEnvelope(json: unknown): boolean {
  return at(json, "jsonrpc") === "2.0";
}

async function text(response: Response): Promise<string | Answer> {
  const bytes = await bodyBytes(response, MAX_BYTES);
  if (typeof bytes !== "string") return new TextDecoder().decode(bytes);
  return {
    kind: "unknown",
    reason: "transport_error",
    why: `the response body could not be read: ${bytes}`,
  };
}

/** The whole body, or why it could not be read. Over `maxBytes` the read is abandoned. */
async function bodyBytes(
  response: Response,
  maxBytes: number,
): Promise<Uint8Array | string> {
  const reader = response.body?.getReader();
  if (reader === undefined) return new Uint8Array(0);
  const parts: Uint8Array[] = [];
  let total = 0;
  try {
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done === true) break;
      total += chunk.value.length;
      if (total > maxBytes) {
        await reader.cancel();
        return `more than ${maxBytes} bytes`;
      }
      parts.push(chunk.value);
    }
  } catch (error) {
    return message(error);
  } finally {
    reader.releaseLock();
  }
  const joined = new Uint8Array(total);
  let offset = 0;
  for (const part of parts) {
    joined.set(part, offset);
    offset += part.length;
  }
  return joined;
}

/** A peer's SSE stream as parsed `StreamResponse` items; an unparsable item ends the stream. */
async function* stream(response: Response): AsyncGenerator<StreamResponse> {
  const body = response.body;
  if (body === null) return;
  for await (const event of sseEvents(body)) {
    const json = parseJson(event.data);
    if (!json.ok) return;
    const payload = isEnvelope(json.value) ? resultOf(json.value) : json.value;
    if (payload === undefined) return;
    const item = StreamItem.safeParse(payload);
    if (!item.success) return;
    yield item.data;
  }
}

function resultOf(json: unknown): unknown {
  const outcome = rpcOutcome(json);
  return "fault" in outcome ? undefined : outcome.result;
}
