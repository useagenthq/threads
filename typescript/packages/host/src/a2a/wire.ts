import {
  A2A_JSON,
  type A2aFault,
  type Binding,
  errorInfo,
  fault,
  httpStatus,
  jsonRpcCode,
  type RpcId,
  rpcFault,
  rpcResult,
  type SseFrame,
  sseBody,
  VERSION_HEADER,
} from "@threadsai/a2a/protocol";

// How an answer and a refusal look on each binding. The status and the code always come from the
// protocol core's pinned table, never from a number written here.

/** What a request was answered as, before it becomes a Response. */
export type Answer =
  | { readonly kind: "value"; readonly value: unknown }
  | { readonly kind: "stream"; readonly frames: AsyncGenerator<SseFrame> };

export type Envelope = {
  readonly binding: Binding;
  /** The JSON-RPC request's id, echoed back; undefined on the HTTP+JSON binding. */
  readonly id: RpcId | undefined;
};

export function answer(envelope: Envelope, value: unknown): Response {
  return envelope.binding === "JSONRPC"
    ? new Response(rpcResult(envelope.id, value), {
        status: 200,
        headers: { "content-type": "application/json" },
      })
    : new Response(JSON.stringify(value), {
        status: 200,
        headers: { "content-type": A2A_JSON },
      });
}

/**
 * The HTTP+JSON error body: a `google.rpc.Status` whose `details` carry the `google.rpc.ErrorInfo`
 * the binding requires. The status names the error and the reason names it again in the form the
 * other binding uses, so a partner reading either one agrees with us; `code` stays the JSON-RPC
 * code, which is the number both of our bindings and the pinned table speak.
 */
function httpBody(f: A2aFault): string {
  return JSON.stringify({
    code: jsonRpcCode(f.name),
    message: `${f.name}: ${f.message}`,
    details: [errorInfo(f.name)],
  });
}

/**
 * A refusal. JSON-RPC answers HTTP 200 with the error in its envelope; HTTP+JSON answers the
 * status from the pinned table with a body carrying the same JSON-RPC code.
 */
export function refuse(envelope: Envelope, f: A2aFault): Response {
  return envelope.binding === "JSONRPC"
    ? new Response(rpcFault(envelope.id, f), {
        status: 200,
        headers: { "content-type": "application/json" },
      })
    : new Response(httpBody(f), {
        status: httpStatus(f.name),
        headers: { "content-type": A2A_JSON },
      });
}

/**
 * No principal: HTTP 401 with a challenge, and the binding's own error envelope. A 401 is the one
 * refusal that leaves the binding's usual status behind, because a client has to see it.
 */
export function unauthenticated(envelope: Envelope): Response {
  const f = fault("InvalidRequestError", "this request is not authenticated");
  const body =
    envelope.binding === "JSONRPC" ? rpcFault(envelope.id, f) : httpBody(f);
  return new Response(body, {
    status: 401,
    headers: {
      "content-type":
        envelope.binding === "JSONRPC" ? "application/json" : A2A_JSON,
      "www-authenticate": "Bearer",
    },
  });
}

/** A streamed answer: each SSE item is the binding's own form of one StreamResponse. */
export function streamed(frames: AsyncGenerator<SseFrame>): Response {
  return new Response(sseBody(frames), {
    status: 200,
    headers: {
      "content-type": "text/event-stream",
      "cache-control": "no-cache",
      "x-accel-buffering": "no",
    },
  });
}

/** One stream item as its `data:` line: wrapped in the JSON-RPC envelope, or bare. */
export function itemOf(envelope: Envelope): (item: unknown) => string {
  return envelope.binding === "JSONRPC"
    ? (item) => rpcResult(envelope.id, item)
    : (item) => JSON.stringify(item);
}

/** The version a request declares: the header, else the query parameter the spec also allows. */
export function versionOf(
  url: URL,
  request: Request,
): {
  readonly header: string | null;
  readonly query: string | null;
} {
  return {
    header: request.headers.get(VERSION_HEADER),
    query: url.searchParams.get(VERSION_HEADER),
  };
}
