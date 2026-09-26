import { JsonValue } from "@threads/core/adapter";
import { type A2aFault, fault } from "./errors";
import { type Method, methodOf } from "./jsonrpc";
import { A2A_VERSION } from "./version";

// Where each operation lives in each binding, and how a request's fields ride there. One table for
// both directions: the client builds a request from it, and the exposed side matches a path with it.

export const BINDINGS = ["JSONRPC", "HTTP+JSON"] as const;
export type Binding = (typeof BINDINGS)[number];

export function bindingOf(protocolBinding: string): Binding | undefined {
  return BINDINGS.find((b) => b === protocolBinding);
}

export type Http = {
  readonly verb: "GET" | "POST";
  /** The path under the interface's base URL, with `{id}` where the task id goes. */
  readonly path: string;
  /** Whether the request message rides in the body (POST) or the query string (GET). */
  readonly where: "body" | "query";
  readonly streams: boolean;
};

export const HTTP: Readonly<Record<Method, Http>> = {
  SendMessage: {
    verb: "POST",
    path: "/message:send",
    where: "body",
    streams: false,
  },
  SendStreamingMessage: {
    verb: "POST",
    path: "/message:stream",
    where: "body",
    streams: true,
  },
  GetTask: { verb: "GET", path: "/tasks/{id}", where: "query", streams: false },
  ListTasks: { verb: "GET", path: "/tasks", where: "query", streams: false },
  CancelTask: {
    verb: "POST",
    path: "/tasks/{id}:cancel",
    where: "body",
    streams: false,
  },
  // The proto's HTTP annotation says GET; the specification's prose table says POST. The proto is
  // normative, so we send GET and accept both inbound (spec/schema/a2a/README.md).
  SubscribeToTask: {
    verb: "GET",
    path: "/tasks/{id}:subscribe",
    where: "query",
    streams: true,
  },
};

/** The two operations whose answer is an SSE stream. */
export function streams(method: Method): boolean {
  return HTTP[method].streams;
}

/** The pinned interface a card chose: where to send, and in which binding. */
export type Wire = {
  readonly url: string;
  readonly binding: Binding;
};

/**
 * The HTTP request one call becomes. `id` is taken out of the message for the path; every other
 * field of a GET operation becomes a query parameter, with `pageSize` and the rest as their JSON.
 */
export type Outbound = {
  readonly url: string;
  readonly verb: "GET" | "POST";
  readonly body: string | undefined;
  readonly accept: string;
};

export function outbound(
  wire: Wire,
  method: Method,
  params: Readonly<Record<string, unknown>>,
): Outbound {
  const accept = streams(method)
    ? "text/event-stream"
    : "application/a2a+json, application/json";
  if (wire.binding === "JSONRPC")
    return {
      url: wire.url,
      verb: "POST",
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
      accept,
    };
  const shape = HTTP[method];
  const base = wire.url.replace(/\/+$/, "");
  const path = shape.path.replace("{id}", () =>
    encodeURIComponent(idOf(params)),
  );
  if (shape.where === "body")
    return {
      url: `${base}${path}`,
      verb: shape.verb,
      body: JSON.stringify(params),
      accept,
    };
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (key === "id" || value === undefined) continue;
    query.set(key, typeof value === "string" ? value : JSON.stringify(value));
  }
  // Also as a query parameter, which the spec allows and some clients prefer.
  query.set("A2A-Version", A2A_VERSION);
  return {
    url: `${base}${path}?${query.toString()}`,
    verb: shape.verb,
    body: undefined,
    accept,
  };
}

function idOf(params: Readonly<Record<string, unknown>>): string {
  const id = params["id"];
  return typeof id === "string" ? id : "";
}

/** A body as JSON, or the JSONParseError it earns. Never `unknown | A2aFault`: a legitimate
 * payload can have a `name` field of its own, so the two are told apart by tag, not by shape. */
export function parseJson(
  text: string,
):
  | { readonly ok: true; readonly value: unknown }
  | { readonly ok: false; readonly fault: A2aFault } {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return {
      ok: false,
      fault: fault("JSONParseError", "the payload is not valid JSON"),
    };
  }
  return JsonValue.safeParse(value).success
    ? { ok: true, value }
    : {
        ok: false,
        fault: fault(
          "JSONParseError",
          "the payload is not JSON we can represent",
        ),
      };
}

/**
 * The operation an inbound HTTP+JSON path names, with the task id it carries. `null` means the path
 * is not one of ours; a known path with the wrong verb is a MethodNotFoundError at the route.
 */
export function inboundPath(
  rest: string,
): { readonly method: Method; readonly id: string | undefined } | null {
  if (rest === "/message:send") return { method: "SendMessage", id: undefined };
  if (rest === "/message:stream")
    return { method: "SendStreamingMessage", id: undefined };
  if (rest === "/tasks") return { method: "ListTasks", id: undefined };
  const task = /^\/tasks\/([^/]+?)(:cancel|:subscribe)?$/.exec(rest);
  const raw = task?.[1];
  if (task === null || raw === undefined) return null;
  const id = decoded(raw);
  if (id === undefined) return null;
  const suffix = task[2];
  const named =
    suffix === ":cancel"
      ? "CancelTask"
      : suffix === ":subscribe"
        ? "SubscribeToTask"
        : "GetTask";
  const method = methodOf(named);
  return method === undefined ? null : { method, id };
}

function decoded(raw: string): string | undefined {
  try {
    return decodeURIComponent(raw);
  } catch {
    return undefined;
  }
}
