import { liveTransport, vet, type WebTransport } from "threadsai/adapter";
import { type Answer, bodyBytes, message, single, stream } from "./answer";
import type { Method } from "./jsonrpc";
import {
  A2A_JSON,
  A2A_VERSION,
  EXTENSIONS_HEADER,
  VERSION_HEADER,
} from "./version";
import { outbound, sentRpcId, streams, type Wire } from "./wire";

// One A2A request goes out. The adapter owns the bytes; which of the outcomes in ./answer the
// caller gets is decided there, and everything here is about getting the request written and
// classifying a transport failure honestly.

export type Sending = {
  readonly transport?: WebTransport;
  /** Resolved at send time from a secret, sent only in the header, never stored. */
  readonly authorization?: string;
  /** The extensions this request relies on, sent as `A2A-Extensions`. */
  readonly extensions?: readonly string[];
  /**
   * The exact request body to send, instead of serializing `params`: a re-dispatch replays the
   * bytes its first attempt stored, so a peer that deduplicates on `messageId` sees one message
   * (30-a2a decision H30-1). Ignored by an operation whose request has no body.
   */
  readonly body?: string;
  readonly signal: AbortSignal;
  readonly timeoutMs: number;
};

export async function call(
  wire: Wire,
  method: Method,
  params: Readonly<Record<string, unknown>>,
  sending: Sending,
): Promise<Answer> {
  // One id per request, checked on the way back: an answer proves it answers us before it is read.
  const built = outbound(wire, method, params, crypto.randomUUID());
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
      ...(built.body === undefined ? {} : { body: sending.body ?? built.body }),
      signal,
    });
  } catch (error) {
    return failed(error, sending.signal.aborted);
  }
  const sent = sentRpcId(built, sending.body);
  return streams(method) && response.status === 200 && isEventStream(response)
    ? { kind: "stream", items: stream(response, sent) }
    : await single(response, sent);
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
