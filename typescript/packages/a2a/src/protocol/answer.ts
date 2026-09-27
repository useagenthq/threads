import {
  type A2aErrorName,
  type A2aFault,
  errorByCode,
  errorInfoIn,
  fault,
  httpStatus,
} from "./errors";
import { type RpcId, rpcOutcome } from "./jsonrpc";
import { sseEvents } from "./sse";
import { StreamResponse as StreamItem, type StreamResponse } from "./task";
import { parseJson } from "./wire";

// Reading a peer's answer. The four outcomes a caller sees are declared here with the reading that
// produces them, so "what a peer can answer" is one file and "how one request goes out" is another.
//
//   ok        the peer answered; the result is parsed a layer up
//   fault     the peer answered an A2A error, which is an answer, not a doubt
//   not_sent  the connection was refused or the TLS handshake failed: no byte was written
//   unknown   anything else after dispatch: a timeout, or a connection that opened and broke
//
// `not_sent` is deliberately narrow. Treating a doubtful case as "not sent" would let an effect
// repeat silently (invariant 3), so only errors that happen strictly before the request is written
// count, and everything else is uncertainty.

/**
 * One item off a peer's stream: a parsed `StreamResponse`, or the refusal that ended the stream. A
 * corrupt frame is a value the caller sees, because a stream that just stops cannot be told apart
 * from one that finished, and "the peer sent us nonsense" is not the same answer as "the peer is
 * done".
 */
export type Streamed =
  | { readonly kind: "item"; readonly item: StreamResponse }
  | { readonly kind: "refused"; readonly fault: A2aFault };

export type Answer =
  | { readonly kind: "ok"; readonly value: unknown }
  | { readonly kind: "stream"; readonly items: AsyncGenerator<Streamed> }
  | { readonly kind: "fault"; readonly fault: A2aFault }
  | { readonly kind: "not_sent"; readonly why: string }
  | {
      readonly kind: "unknown";
      readonly reason: "timeout" | "transport_error";
      readonly why: string;
    };

/** A response body over this many bytes is refused: a card or a task is small. */
export const MAX_BYTES: number = 1 << 20;

/** A non-streaming answer: the operation's own message, or the A2A error the peer named. */
export async function single(
  response: Response,
  sent: RpcId | undefined,
): Promise<Answer> {
  const read = await text(response);
  if (typeof read !== "string") return read;
  const json = parseJson(read);
  if (!json.ok) return { kind: "fault", fault: json.fault };
  // Both bindings can answer either way, so a JSON-RPC envelope is unwrapped wherever it appears.
  if (isEnvelope(json.value)) {
    const outcome = rpcOutcome(json.value, sent);
    return "fault" in outcome
      ? { kind: "fault", fault: outcome.fault }
      : { kind: "ok", value: outcome.result };
  }
  if (response.status >= 400)
    return { kind: "fault", fault: httpFault(response.status, json.value) };
  return { kind: "ok", value: json.value };
}

/**
 * An HTTP+JSON error body. The binding answers a `google.rpc.Status`, whose `details` MUST carry a
 * `google.rpc.ErrorInfo`: the reason is what names the error there, so a body without one we
 * recognise is a response we cannot read rather than an error we can act on. The JSON-RPC code is
 * read too, where an implementation puts one, and the two must agree with each other and with the
 * status the pinned table gives that error.
 */
function httpFault(status: number, json: unknown): A2aFault {
  const found = errorInfoIn(at(json, "details"));
  if (found === undefined)
    return fault(
      "InvalidAgentResponseError",
      `the peer answered HTTP ${status} with no google.rpc.ErrorInfo`,
    );
  const named: A2aErrorName | undefined = found.named;
  if (named === undefined)
    return fault(
      "InvalidAgentResponseError",
      `the peer answered HTTP ${status} with ErrorInfo reason ${found.info.reason}, which is not an A2A error`,
    );
  // A 401 is the one status that overrides the table: it is a challenge a client has to see, so
  // whatever error the peer names, the status it arrives with is 401 rather than that error's own.
  if (status !== 401 && httpStatus(named) !== status)
    return fault(
      "InvalidAgentResponseError",
      `the peer answered HTTP ${status} with ErrorInfo reason ${found.info.reason}, which is HTTP ${httpStatus(named)}`,
    );
  const byCode = errorByCode(codeIn(json) ?? Number.NaN);
  if (byCode !== undefined && byCode !== named)
    return fault(
      "InvalidAgentResponseError",
      `the peer answered HTTP ${status} with code ${codeIn(json)} and ErrorInfo reason ${found.info.reason}, which name different errors`,
    );
  return fault(named, messageIn(json) ?? `the peer answered HTTP ${status}`);
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

/**
 * The body as text, decoded fatally: bytes that are not UTF-8 are a fault, never a U+FFFD we read
 * on as if the peer had sent it. The peer did answer, so a body we cannot decode is an answer we
 * cannot read and nothing is in doubt about whether it received us.
 */
async function text(response: Response): Promise<string | Answer> {
  const bytes = await bodyBytes(response, MAX_BYTES);
  if (typeof bytes === "string")
    return {
      kind: "unknown",
      reason: "transport_error",
      why: `the response body could not be read: ${bytes}`,
    };
  try {
    return new TextDecoder(undefined, { fatal: true }).decode(bytes);
  } catch {
    return {
      kind: "fault",
      fault: fault(
        "InvalidAgentResponseError",
        "the response body is not valid UTF-8",
      ),
    };
  }
}

/** An error's text, for a `why` a person reads. */
export function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** The whole body, or why it could not be read. Over `maxBytes` the read is abandoned. */
export async function bodyBytes(
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

/**
 * A peer's SSE stream as parsed `StreamResponse` items. Every way the stream can stop being
 * readable — a frame that is not JSON, an envelope that answers someone else, a payload that is not
 * a `StreamResponse`, a body that is not UTF-8, a frame over the budget — ends it with a refusal the
 * caller can see, so a corrupt stream is never mistaken for a finished one.
 */
export async function* stream(
  response: Response,
  sent: RpcId | undefined,
): AsyncGenerator<Streamed> {
  const body = response.body;
  if (body === null) return;
  for await (const read of sseEvents(body)) {
    if (read.kind === "refused") {
      yield {
        kind: "refused",
        fault: fault("InvalidAgentResponseError", read.why),
      };
      return;
    }
    const item = frameOf(read.event.data, sent);
    yield item;
    if (item.kind === "refused") return;
  }
}

/** One `data:` line as a stream item, or the refusal it earns. */
function frameOf(data: string, sent: RpcId | undefined): Streamed {
  const json = parseJson(data);
  if (!json.ok) return { kind: "refused", fault: json.fault };
  if (isEnvelope(json.value)) {
    const outcome = rpcOutcome(json.value, sent);
    if ("fault" in outcome) return { kind: "refused", fault: outcome.fault };
    const inner = StreamItem.safeParse(outcome.result);
    return inner.success
      ? { kind: "item", item: inner.data }
      : { kind: "refused", fault: notAStreamItem() };
  }
  const item = StreamItem.safeParse(json.value);
  return item.success
    ? { kind: "item", item: item.data }
    : { kind: "refused", fault: notAStreamItem() };
}

function notAStreamItem(): A2aFault {
  return fault(
    "InvalidAgentResponseError",
    "a stream frame is not a StreamResponse",
  );
}
