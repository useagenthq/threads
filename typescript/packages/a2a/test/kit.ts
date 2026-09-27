import type { Sent, WebTransport } from "threadsai/adapter";
import { type A2aErrorName, errorInfo } from "../src/protocol/errors";
import type { Wire } from "../src/protocol/wire";

// The peers the client tests drive. A transport here records what it was asked to send and answers
// like a real peer would — an envelope has to echo the id it was asked with, because a transport
// that answered a fixed id could not tell a client that checks correlation from one that does not.

export const RPC: Wire = {
  url: "https://partner.example/a2a/refunds",
  binding: "JSONRPC",
};

export const REST: Wire = {
  url: "https://partner.example/a2a/refunds",
  binding: "HTTP+JSON",
};

export const TASK: {
  readonly id: string;
  readonly contextId: string;
  readonly status: { readonly state: string };
} = {
  id: "task-1",
  contextId: "ctx-1",
  status: { state: "TASK_STATE_WORKING" },
};

export const STATUS_UPDATE: string =
  '{"statusUpdate":{"taskId":"task-1","contextId":"ctx-1","status":{"state":"TASK_STATE_COMPLETED"}}}';

export type Sending = {
  readonly transport: WebTransport;
  readonly signal: AbortSignal;
  readonly timeoutMs: number;
};

export function sending(transport: WebTransport): Sending {
  return {
    transport,
    signal: new AbortController().signal,
    timeoutMs: 5_000,
  };
}

/** A transport that records what it was asked to send. */
export type Recording = WebTransport & {
  readonly sent: { url: string; init: Sent }[];
};

/** A transport that answers one response, and records what it was asked to send. */
export function answering(
  make: (url: string, init: Sent) => Response | Promise<Response>,
): Recording {
  const sent: { url: string; init: Sent }[] = [];
  return {
    sent,
    resolve: async () => ["93.184.216.34"],
    fetch: async (url, _address, init) => {
      sent.push({ url, init });
      return await make(url, init);
    },
  };
}

/** A transport that fails the request with a node-style error code. */
export function failing(
  code: string | undefined,
  name = "Error",
): WebTransport {
  return {
    resolve: async () => ["93.184.216.34"],
    fetch: async () => {
      const error = new Error(`transport said ${code ?? name}`);
      error.name = name;
      if (code !== undefined) Object.assign(error, { code });
      throw error;
    },
  };
}

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** The JSON-RPC id a request carried. A peer has to echo this, and ours has to check that it did. */
export function sentId(init: Sent): unknown {
  const body: unknown = JSON.parse(init.body ?? "{}");
  return typeof body === "object" && body !== null && "id" in body
    ? body.id
    : undefined;
}

/** A peer that answers a result in the envelope, echoing the id it was asked with. */
export function rpcOk(result: unknown): Recording {
  return answering((_url, init) =>
    json({ jsonrpc: "2.0", id: sentId(init), result }),
  );
}

/** A peer that answers an error in the envelope, echoing the id it was asked with. */
export function rpcErr(error: unknown): Recording {
  return answering((_url, init) =>
    json({ jsonrpc: "2.0", id: sentId(init), error }),
  );
}

/** An HTTP+JSON error body as the binding requires it: a Status whose details name the error. */
export function httpErr(name: A2aErrorName, status: number): Recording {
  return answering(() =>
    json({ code: -1, message: "no", details: [errorInfo(name)] }, status),
  );
}

/** An SSE body whose frames echo the request's id, as a peer streaming the envelope must. */
export function sse(frames: (id: unknown) => readonly string[]): Recording {
  return answering(
    (_url, init) =>
      new Response(
        frames(sentId(init))
          .map((data) => `data: ${data}\n\n`)
          .join(""),
        { headers: { "content-type": "text/event-stream" } },
      ),
  );
}
